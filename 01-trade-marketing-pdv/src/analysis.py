"""
Pipeline de análisis:
  1. Carga los CSV en DuckDB
  2. Ejecuta sql/kpis.sql (vistas de KPIs)
  3. Exporta tablas de resultados, gráficos PNG y el dashboard HTML interactivo
"""
import json
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
DATA, SQL, OUT = ROOT / "data", ROOT / "sql", ROOT / "outputs"
CHARTS, DASH = OUT / "charts", ROOT / "dashboard"
for p in (CHARTS, DASH):
    p.mkdir(parents=True, exist_ok=True)

# paleta sobria: un color principal, un color de alerta, grises de contexto
AZUL, ROJO, GRIS, GRIS_CLARO = "#1F4E79", "#C0392B", "#7F8C8D", "#D5DBDB"
ZONAS_COLOR = {
    "Lima Centro": "#1F4E79",
    "Lima Sur": "#2E86C1",
    "Lima Norte": "#5DADE2",
    "Provincias": "#A9CCE3",
    "Lima Este": "#C0392B",
}

con = duckdb.connect()
for csv in sorted(DATA.glob("*.csv")):
    con.execute(f"CREATE TABLE {csv.stem} AS SELECT * FROM read_csv_auto('{csv.as_posix()}')")
con.execute(SQL.joinpath("kpis.sql").read_text(encoding="utf-8"))

q = lambda s: con.sql(s).df()

# ------------------------------------------------------------------ KPIs globales
kpi = q("""
    SELECT
      (SELECT SUM(visitas_ejec)*1.0/SUM(visitas_plan) FROM fact_visitas)        AS cobertura,
      (SELECT AVG(oos) FROM fact_auditoria)                                     AS oos,
      (SELECT AVG(CASE WHEN ABS(a.precio_observado/s.precio_sugerido-1)<=0.05 THEN 1 ELSE 0 END)
         FROM fact_auditoria a JOIN dim_sku s USING (sku_id))                    AS precio_ok,
      (SELECT AVG(exhibicion_ok) FROM fact_auditoria)                           AS exhibicion,
      (SELECT SUM(venta_soles) FROM fact_sellout)                               AS sellout,
      (SELECT SUM(venta_perdida_soles) FROM venta_perdida_oos)                  AS venta_perdida
""").iloc[0]

tiendas = q("SELECT * FROM scorecard_tienda")
tiendas["grupo_cob"] = pd.cut(
    tiendas.cobertura, [0, 0.70, 0.85, 1.01], labels=["< 70%", "70–85%", "≥ 85%"], right=False
)
por_grupo = (
    tiendas.groupby("grupo_cob", observed=True)
    .agg(tiendas=("tienda_id", "count"), oos=("oos_pct", "mean"))
    .reset_index()
)
oos_sem = q("""
    SELECT semana, zona, AVG(oos_pct) AS oos FROM kpi_oos_semana GROUP BY 1,2 ORDER BY 1
""")
perdida_cat = q("""
    SELECT categoria, SUM(venta_perdida_soles) AS soles
    FROM venta_perdida_oos GROUP BY 1 ORDER BY 2
""")
personal = q("SELECT * FROM scorecard_personal ORDER BY cobertura")
precio = q("SELECT * FROM kpi_precio")

# ------------------------------------------------------------------ resumen para el README
resumen = {
    "cobertura_global": round(kpi.cobertura, 3),
    "oos_global": round(kpi.oos, 3),
    "cumplimiento_precio": round(kpi.precio_ok, 3),
    "exhibicion": round(kpi.exhibicion, 3),
    "sellout_soles": round(kpi.sellout),
    "venta_perdida_soles": round(kpi.venta_perdida),
    "venta_perdida_pct": round(kpi.venta_perdida / kpi.sellout, 3),
    "oos_por_grupo_cobertura": {
        str(r.grupo_cob): {"tiendas": int(r.tiendas), "oos": round(r.oos, 3)}
        for r in por_grupo.itertuples()
    },
    "corr_cobertura_oos": round(tiendas.cobertura.corr(tiendas.oos_pct), 2),
    "perdida_por_categoria": {r.categoria: round(r.soles) for r in perdida_cat.itertuples()},
    "personal_bajo_70": personal[personal.cobertura < 0.70].personal_id.tolist(),
}
(OUT / "resumen_kpis.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False))
tiendas.drop(columns="grupo_cob").to_csv(OUT / "scorecard_tiendas.csv", index=False)
personal.to_csv(OUT / "scorecard_personal.csv", index=False)

# ------------------------------------------------------------------ gráficos PNG
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titleweight": "bold",
        "axes.titlesize": 12,
        "axes.titlelocation": "left",
    }
)
pct = lambda ax, axis="y": getattr(ax, f"{axis}axis").set_major_formatter(
    plt.FuncFormatter(lambda v, _: f"{v:.0%}")
)

# 1. Cobertura vs OOS por tienda
fig, ax = plt.subplots(figsize=(8, 5))
baja = tiendas.cobertura < 0.70
ax.scatter(tiendas.cobertura[~baja], tiendas.oos_pct[~baja], s=40, color=AZUL, alpha=0.75,
           label="Cobertura ≥ 70%")
ax.scatter(tiendas.cobertura[baja], tiendas.oos_pct[baja], s=40, color=ROJO, alpha=0.85,
           label="Cobertura < 70%")
ax.axvline(0.70, color=GRIS, ls="--", lw=1)
pct(ax, "x"); pct(ax, "y")
ax.set_xlabel("Cobertura de ruta (visitas ejecutadas / planificadas)")
ax.set_ylabel("Out-of-Stock promedio")
ax.set_title(f"Menos visitas, más quiebres: correlación {resumen['corr_cobertura_oos']}")
ax.legend(frameon=False)
fig.tight_layout(); fig.savefig(CHARTS / "01_cobertura_vs_oos.png", dpi=160); plt.close(fig)

# 2. Tendencia de OOS: media móvil de 4 semanas por zona
oos_mm = (
    oos_sem.pivot(index="semana", columns="zona", values="oos")
    .rolling(4, min_periods=4).mean().dropna()
)
fig, ax = plt.subplots(figsize=(9, 5))
for zona in oos_mm.columns:
    destacada = zona in ("Lima Este", "Lima Centro")
    ax.plot(oos_mm.index, oos_mm[zona], color=ZONAS_COLOR[zona] if destacada else GRIS_CLARO,
            lw=2.6 if destacada else 1.4, zorder=3 if destacada else 1)
    ax.annotate(zona, (oos_mm.index[-1], oos_mm[zona].iloc[-1]), xytext=(6, 0),
                textcoords="offset points", va="center", fontsize=9,
                color=ZONAS_COLOR[zona] if destacada else GRIS)
pct(ax)
ax.xaxis.set_major_formatter(__import__("matplotlib.dates").dates.DateFormatter("%b"))
ax.set_ylabel("Out-of-Stock (media móvil 4 semanas)")
ax.set_title("Lima Este sostiene el OOS más alto; Lima Centro, el más bajo")
ax.set_xlim(oos_mm.index[0], oos_mm.index[-1] + pd.Timedelta(days=24))
fig.tight_layout(); fig.savefig(CHARTS / "02_oos_semanal_zona.png", dpi=160); plt.close(fig)

# 3. Venta perdida por categoría
fig, ax = plt.subplots(figsize=(8, 4))
bars = ax.barh(perdida_cat.categoria, perdida_cat.soles / 1e3,
               color=[GRIS_CLARO] * (len(perdida_cat) - 1) + [ROJO])
ax.bar_label(bars, labels=[f"S/ {v/1e3:,.0f} mil" for v in perdida_cat.soles], padding=4)
ax.set_xlabel("Venta perdida estimada (miles de S/)")
ax.set_title("Cuidado personal concentra la mitad de la venta perdida por OOS")
ax.set_xlim(0, perdida_cat.soles.max() / 1e3 * 1.25)
fig.tight_layout(); fig.savefig(CHARTS / "03_venta_perdida_categoria.png", dpi=160); plt.close(fig)

# 4. Ranking de cobertura por persona de campo
fig, ax = plt.subplots(figsize=(8, 8))
colores = [ROJO if c < 0.70 else AZUL for c in personal.cobertura]
ax.barh(personal.personal_id + " · " + personal.rol, personal.cobertura, color=colores)
ax.axvline(0.85, color=GRIS, ls="--", lw=1)
ax.text(0.85, len(personal) - 0.2, "meta 85%", color=GRIS, ha="center", va="bottom", fontsize=9)
pct(ax, "x")
ax.set_title("Cobertura por persona de campo (rojo: bajo 70%)")
fig.tight_layout(); fig.savefig(CHARTS / "04_cobertura_personal.png", dpi=160); plt.close(fig)

# ------------------------------------------------------------------ dashboard interactivo
fig = make_subplots(
    rows=2, cols=2, vertical_spacing=0.14, horizontal_spacing=0.09,
    subplot_titles=(
        "Cobertura vs. Out-of-Stock por tienda",
        "OOS semanal por zona",
        "Venta perdida estimada por categoría (S/)",
        "Cumplimiento de precio por cadena y categoría",
    ),
)
for zona, g in tiendas.groupby("zona"):
    fig.add_trace(go.Scatter(
        x=g.cobertura, y=g.oos_pct, mode="markers", name=zona, legendgroup=zona,
        marker=dict(size=9, color=ZONAS_COLOR[zona]),
        customdata=g[["tienda_id", "cadena", "formato", "venta_soles"]],
        hovertemplate="<b>%{customdata[0]}</b> · %{customdata[1]} · %{customdata[2]}<br>"
                      "Cobertura %{x:.0%} · OOS %{y:.1%}<br>Sell-out S/ %{customdata[3]:,.0f}<extra></extra>",
    ), row=1, col=1)
for zona, g in oos_sem.groupby("zona"):
    fig.add_trace(go.Scatter(
        x=g.semana, y=g.oos, mode="lines", name=zona, legendgroup=zona, showlegend=False,
        line=dict(color=ZONAS_COLOR[zona], width=3 if zona == "Lima Este" else 2),
        hovertemplate=zona + " · %{x|%d %b}: %{y:.1%}<extra></extra>",
    ), row=1, col=2)
fig.add_trace(go.Bar(
    x=perdida_cat.soles, y=perdida_cat.categoria, orientation="h", showlegend=False,
    marker_color=[GRIS_CLARO] * (len(perdida_cat) - 1) + [ROJO],
    text=[f"S/ {v/1e3:,.0f} mil" for v in perdida_cat.soles], textposition="outside",
    hovertemplate="%{y}: S/ %{x:,.0f}<extra></extra>",
), row=2, col=1)
piv = precio.pivot(index="categoria", columns="cadena", values="cumplimiento_precio")
fig.add_trace(go.Heatmap(
    z=piv.values, x=piv.columns, y=piv.index, colorscale="Blues", zmin=0.6, zmax=0.85,
    text=[[f"{v:.0%}" for v in row] for row in piv.values], texttemplate="%{text}",
    showscale=False, hovertemplate="%{y} · %{x}: %{z:.1%}<extra></extra>",
), row=2, col=2)
fig.update_xaxes(tickformat=".0%", row=1, col=1, title_text="Cobertura")
fig.update_yaxes(tickformat=".0%", row=1, col=1, title_text="OOS")
fig.update_yaxes(tickformat=".0%", row=1, col=2)
fig.update_xaxes(range=[0, perdida_cat.soles.max() * 1.3], row=2, col=1)
fig.update_layout(
    height=820, template="plotly_white", margin=dict(t=60, l=40, r=20, b=40),
    font=dict(family="Inter, Segoe UI, sans-serif", size=12),
    legend=dict(orientation="h", y=1.08, x=0),
)

cards = [
    ("Cobertura de ruta", f"{kpi.cobertura:.1%}", "meta 85%", kpi.cobertura < 0.85),
    ("Out-of-Stock", f"{kpi.oos:.1%}", "meta ≤ 6%", kpi.oos > 0.06),
    ("Cumplimiento de precio", f"{kpi.precio_ok:.1%}", "±5% del sugerido", kpi.precio_ok < 0.85),
    ("Exhibición OK", f"{kpi.exhibicion:.1%}", "planograma", False),
    ("Sell-out", f"S/ {kpi.sellout/1e6:,.1f} M", "26 semanas", False),
    ("Venta perdida por OOS", f"S/ {kpi.venta_perdida/1e6:,.2f} M",
     f"{kpi.venta_perdida/kpi.sellout:.1%} del sell-out", True),
]
cards_html = "".join(
    f'<div class="card{" alert" if a else ""}"><span>{t}</span><strong>{v}</strong><small>{s}</small></div>'
    for t, v, s, a in cards
)
html = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard Ejecución PDV</title>
<style>
 body{{margin:0;font-family:Inter,'Segoe UI',sans-serif;background:#F4F6F8;color:#1C2833}}
 header{{background:#1F4E79;color:#fff;padding:22px 32px}}
 header h1{{margin:0;font-size:22px}} header p{{margin:6px 0 0;opacity:.85;font-size:13px}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;padding:22px 32px 0}}
 .card{{background:#fff;border-radius:10px;padding:14px 16px;border-left:4px solid #1F4E79;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
 .card.alert{{border-left-color:#C0392B}}
 .card span{{font-size:12px;color:#5D6D7E}} .card strong{{display:block;font-size:24px;margin:4px 0}}
 .card small{{font-size:11px;color:#7F8C8D}}
 main{{padding:16px 32px 32px}} .panel{{background:#fff;border-radius:10px;padding:8px}}
 footer{{font-size:11px;color:#7F8C8D;padding:0 32px 24px}}
</style></head><body>
<header><h1>Ejecución en punto de venta · Canal moderno</h1>
<p>72 tiendas · 30 personas de campo · 40 SKU · 26 semanas (ene–jun 2026) · datos simulados</p></header>
<section class="cards">{cards_html}</section>
<main><div class="panel">{fig.to_html(full_html=False, include_plotlyjs="cdn")}</div></main>
<footer>Proyecto de portafolio de Josué Valverde · Datos 100% simulados con fines demostrativos ·
Fuente: github.com/JosueRVP/JosueValverde_Analyst</footer>
</body></html>"""
(DASH / "index.html").write_text(html, encoding="utf-8")

print(json.dumps(resumen, indent=2, ensure_ascii=False))
