-- =====================================================================
-- KPIs de ejecución en PDV (DuckDB / sintaxis SQL estándar)
-- Tablas base: dim_tiendas, dim_sku, dim_personal,
--              fact_visitas, fact_auditoria, fact_sellout
-- =====================================================================

-- 1) Cobertura de ruta: visitas ejecutadas / visitas planificadas
CREATE OR REPLACE VIEW kpi_cobertura_semana AS
SELECT
    v.semana,
    t.zona,
    SUM(v.visitas_ejec)                         AS visitas_ejec,
    SUM(v.visitas_plan)                         AS visitas_plan,
    SUM(v.visitas_ejec) * 1.0 / SUM(v.visitas_plan) AS cobertura
FROM fact_visitas v
JOIN dim_tiendas t USING (tienda_id)
GROUP BY 1, 2;

-- 2) Out-of-Stock: % de SKU auditados sin stock en góndola
CREATE OR REPLACE VIEW kpi_oos_semana AS
SELECT
    a.semana,
    t.zona,
    s.categoria,
    AVG(a.oos)            AS oos_pct,
    AVG(a.exhibicion_ok)  AS exhibicion_pct
FROM fact_auditoria a
JOIN dim_tiendas t USING (tienda_id)
JOIN dim_sku     s USING (sku_id)
GROUP BY 1, 2, 3;

-- 3) Cumplimiento de precio: precio observado dentro de ±5% del sugerido
CREATE OR REPLACE VIEW kpi_precio AS
SELECT
    t.cadena,
    s.categoria,
    AVG(CASE WHEN ABS(a.precio_observado / s.precio_sugerido - 1) <= 0.05
             THEN 1 ELSE 0 END)                         AS cumplimiento_precio,
    AVG(a.precio_observado / s.precio_sugerido - 1)     AS desviacion_media
FROM fact_auditoria a
JOIN dim_tiendas t USING (tienda_id)
JOIN dim_sku     s USING (sku_id)
GROUP BY 1, 2;

-- 4) Scorecard por tienda: cobertura, OOS y sell-out del periodo
CREATE OR REPLACE VIEW scorecard_tienda AS
WITH cob AS (
    SELECT tienda_id, SUM(visitas_ejec) * 1.0 / SUM(visitas_plan) AS cobertura
    FROM fact_visitas GROUP BY 1
),
oos AS (
    SELECT tienda_id, AVG(oos) AS oos_pct, AVG(exhibicion_ok) AS exhibicion_pct
    FROM fact_auditoria GROUP BY 1
),
so AS (
    SELECT tienda_id, SUM(venta_soles) AS venta_soles, SUM(unidades) AS unidades
    FROM fact_sellout GROUP BY 1
)
SELECT t.*, cob.cobertura, oos.oos_pct, oos.exhibicion_pct, so.venta_soles, so.unidades
FROM dim_tiendas t
JOIN cob USING (tienda_id)
JOIN oos USING (tienda_id)
JOIN so  USING (tienda_id);

-- 5) Productividad de la fuerza de campo
CREATE OR REPLACE VIEW scorecard_personal AS
SELECT
    p.personal_id,
    p.rol,
    p.zona,
    COUNT(DISTINCT t.tienda_id)                      AS tiendas_asignadas,
    AVG(sc.cobertura)                                AS cobertura,
    AVG(sc.oos_pct)                                  AS oos_pct,
    SUM(sc.venta_soles)                              AS venta_soles
FROM dim_personal p
JOIN dim_tiendas t      USING (personal_id)
JOIN scorecard_tienda sc USING (tienda_id)
GROUP BY 1, 2, 3;

-- 6) Venta perdida estimada por quiebre de stock
--    Se usa la venta promedio del SKU en la tienda cuando SÍ hubo stock
--    como estimación de lo que se habría vendido en las semanas con OOS.
CREATE OR REPLACE VIEW venta_perdida_oos AS
WITH base AS (
    SELECT a.semana, a.tienda_id, a.sku_id, a.oos, f.venta_soles
    FROM fact_auditoria a
    JOIN fact_sellout   f USING (semana, tienda_id, sku_id)
),
promedio_con_stock AS (
    SELECT tienda_id, sku_id, AVG(venta_soles) AS venta_prom
    FROM base WHERE oos = 0
    GROUP BY 1, 2
)
SELECT
    t.zona,
    s.categoria,
    SUM(GREATEST(p.venta_prom - b.venta_soles, 0)) AS venta_perdida_soles
FROM base b
JOIN promedio_con_stock p USING (tienda_id, sku_id)
JOIN dim_tiendas t USING (tienda_id)
JOIN dim_sku     s USING (sku_id)
WHERE b.oos = 1
GROUP BY 1, 2;
