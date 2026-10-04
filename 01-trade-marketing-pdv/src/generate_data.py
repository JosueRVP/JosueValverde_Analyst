"""
Genera un dataset SIMULADO de ejecución en punto de venta (PDV) para un
fabricante de consumo masivo que trabaja con una agencia de trade marketing.

Los datos son ficticios: no provienen de ninguna empresa real. Están diseñados
para reproducir relaciones típicas del canal moderno:
  - menos visitas ejecutadas  -> más quiebres de stock (OOS)
  - más OOS                   -> menos sell-out
  - precio fuera de rango     -> menor rotación

Salida: CSV en ../data/
"""
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 2026
N_WEEKS = 26
START = pd.Timestamp("2026-01-05")  # lunes

rng = np.random.default_rng(SEED)
OUT = Path(__file__).resolve().parents[1] / "data"
OUT.mkdir(exist_ok=True)

# ---------------------------------------------------------------- dimensiones
zonas = {
    "Lima Norte": 18,
    "Lima Sur": 16,
    "Lima Centro": 14,
    "Lima Este": 12,
    "Provincias": 12,
}
cadenas = ["Cadena A", "Cadena B", "Cadena C"]
formatos = ["Hiper", "Super", "Express"]

tiendas = []
tid = 1
for zona, n in zonas.items():
    for _ in range(n):
        fmt = rng.choice(formatos, p=[0.25, 0.5, 0.25])
        tiendas.append(
            {
                "tienda_id": f"T{tid:03d}",
                "cadena": rng.choice(cadenas, p=[0.5, 0.3, 0.2]),
                "formato": fmt,
                "zona": zona,
                # tamaño relativo de la tienda (afecta demanda base)
                "factor_trafico": round(
                    {"Hiper": 1.8, "Super": 1.0, "Express": 0.55}[fmt]
                    * rng.uniform(0.8, 1.2),
                    2,
                ),
            }
        )
        tid += 1
dim_tiendas = pd.DataFrame(tiendas)

categorias = {
    "Pilas": (8, 6.5, 14.0),
    "Cuidado personal": (12, 35.0, 120.0),
    "Mascotas": (10, 12.0, 45.0),
    "Hogar": (10, 9.0, 30.0),
}
skus = []
sid = 1
for cat, (n, pmin, pmax) in categorias.items():
    for i in range(n):
        precio = round(rng.uniform(pmin, pmax), 1)
        skus.append(
            {
                "sku_id": f"S{sid:03d}",
                "categoria": cat,
                "descripcion": f"{cat} {i + 1:02d}",
                "precio_sugerido": precio,
                "rotacion_base": round(rng.uniform(4, 25) * (60 / precio) ** 0.35, 2),
            }
        )
        sid += 1
dim_sku = pd.DataFrame(skus)

# 30 personas de campo: 18 mercaderistas (por horas) y 12 promotores (8 h fijas)
personal = []
for i in range(30):
    rol = "Mercaderista" if i < 18 else "Promotor"
    personal.append(
        {
            "personal_id": f"P{i + 1:02d}",
            "rol": rol,
            "zona": list(zonas)[i % len(zonas)],
            # disciplina de ruta: cuántas visitas planificadas realmente ejecuta
            "disciplina": round(rng.beta(8, 2) if rol == "Promotor" else rng.beta(6, 2.5), 3),
        }
    )
dim_personal = pd.DataFrame(personal)

# asignación tienda -> responsable (dentro de su zona)
asig = []
for zona, grupo in dim_tiendas.groupby("zona"):
    resp = dim_personal[dim_personal.zona == zona].personal_id.tolist()
    for k, t in enumerate(grupo.tienda_id):
        asig.append({"tienda_id": t, "personal_id": resp[k % len(resp)]})
dim_tiendas = dim_tiendas.merge(pd.DataFrame(asig), on="tienda_id")

# ---------------------------------------------------------------- visitas
semanas = [START + pd.Timedelta(weeks=w) for w in range(N_WEEKS)]
vis = []
disc = dim_personal.set_index("personal_id").disciplina
for s in semanas:
    for t in dim_tiendas.itertuples():
        plan = 3 if t.formato == "Hiper" else 2
        p = np.clip(disc[t.personal_id] + rng.normal(0, 0.07), 0.2, 1.0)
        ejec = rng.binomial(plan, p)
        vis.append(
            {
                "semana": s.date(),
                "tienda_id": t.tienda_id,
                "personal_id": t.personal_id,
                "visitas_plan": plan,
                "visitas_ejec": ejec,
            }
        )
fact_visitas = pd.DataFrame(vis)

# ---------------------------------------------------------------- auditoría + sell-out
cob = (
    fact_visitas.assign(cob=lambda d: d.visitas_ejec / d.visitas_plan)
    .set_index(["semana", "tienda_id"])
    .cob
)
estac = {w: 1 + 0.18 * np.sin(2 * np.pi * (w - 2) / N_WEEKS) for w in range(N_WEEKS)}

aud_rows, so_rows = [], []
for w, s in enumerate(semanas):
    sd = s.date()
    for t in dim_tiendas.itertuples():
        c = cob[(sd, t.tienda_id)]
        # probabilidad de quiebre: base 4% + castigo por baja cobertura
        p_oos = 0.04 + 0.22 * (1 - c) ** 1.5 + (0.03 if t.formato == "Express" else 0)
        oos = rng.random(len(dim_sku)) < p_oos
        # desviación de precio: más frecuente sin visita
        dev = rng.normal(0, 0.03 + 0.06 * (1 - c), len(dim_sku))
        precio_obs = (dim_sku.precio_sugerido.values * (1 + dev)).round(1)
        exhib = rng.random(len(dim_sku)) < (0.55 + 0.4 * c)
        for j, sk in enumerate(dim_sku.itertuples()):
            aud_rows.append(
                (sd, t.tienda_id, sk.sku_id, int(oos[j]), precio_obs[j], int(exhib[j]))
            )
            demanda = sk.rotacion_base * t.factor_trafico * estac[w]
            demanda *= 1.12 if exhib[j] else 1.0
            demanda *= 1 - 1.4 * max(dev[j], 0)  # sobreprecio frena la venta
            unidades = 0 if oos[j] and rng.random() < 0.7 else rng.poisson(max(demanda, 0.1))
            so_rows.append((sd, t.tienda_id, sk.sku_id, unidades, round(unidades * precio_obs[j], 2)))

fact_auditoria = pd.DataFrame(
    aud_rows,
    columns=["semana", "tienda_id", "sku_id", "oos", "precio_observado", "exhibicion_ok"],
)
fact_sellout = pd.DataFrame(
    so_rows, columns=["semana", "tienda_id", "sku_id", "unidades", "venta_soles"]
)

for name, df in {
    "dim_tiendas": dim_tiendas,
    "dim_sku": dim_sku,
    "dim_personal": dim_personal,
    "fact_visitas": fact_visitas,
    "fact_auditoria": fact_auditoria,
    "fact_sellout": fact_sellout,
}.items():
    df.to_csv(OUT / f"{name}.csv", index=False)
    print(f"{name:16s} {len(df):>7,} filas")
