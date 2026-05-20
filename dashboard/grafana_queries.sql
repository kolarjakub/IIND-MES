-- =============================================================================
-- GRAFANA SQL QUERIES — MES Dashboard
-- PostgreSQL data source, schema: mes
-- =============================================================================


-- =============================================================================
-- MACHINE STATISTICS
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Total operating time + occupation % per machine  (Stat / Bar Gauge panel)
--    Use latest snapshot per machine (current cumulative values)
-- -----------------------------------------------------------------------------
SELECT
    m.name                               AS machine,
    ms.total_op_time_s                   AS "Total Op Time (s)",
    ROUND(ms.occupation_pct::numeric, 1) AS "Occupation (%)"
FROM mes.machines m
LEFT JOIN mes.machine_stats ms
    ON ms.machine_id = m.machine_id
    AND ms.recorded_at = (
        SELECT MAX(recorded_at)
        FROM mes.machine_stats
        WHERE machine_id = m.machine_id
    )
ORDER BY m.name;


-- -----------------------------------------------------------------------------
-- 2. Occupation % over time — time series per machine  (Time series panel)
--    Grafana: set "Time column" = recorded_at, "Metric" = machine
-- -----------------------------------------------------------------------------
SELECT
    ms.recorded_at                       AS "time",
    m.name                               AS metric,
    ROUND(ms.occupation_pct::numeric, 1) AS "Occupation (%)"
FROM mes.machine_stats ms
JOIN mes.machines m USING (machine_id)
WHERE ms.recorded_at BETWEEN $__timeFrom() AND $__timeTo()
ORDER BY ms.recorded_at, m.name;


-- -----------------------------------------------------------------------------
-- 3. Total operating time per tool per machine  (Bar chart panel)
--    Rows: machine+tool   Value: total_time_s
-- -----------------------------------------------------------------------------
SELECT
    m.name                               AS machine,
    tu.tool_name                         AS tool,
    ROUND(tu.total_time_s::numeric, 1)   AS "Total Time (s)"
FROM mes.tool_usage tu
JOIN mes.machines m USING (machine_id)
ORDER BY m.name, tu.tool_name;


-- -----------------------------------------------------------------------------
-- 4. Tool usage over time — time series  (Time series panel)
--    Shows how cumulative tool time grows across the simulation
--    Grafana: set "Time column" = updated_at, group by machine+tool
-- -----------------------------------------------------------------------------
SELECT
    tu.updated_at                        AS "time",
    m.name || '/' || tu.tool_name        AS metric,
    ROUND(tu.total_time_s::numeric, 1)   AS "Cumulative Time (s)"
FROM mes.tool_usage tu
JOIN mes.machines m USING (machine_id)
WHERE tu.updated_at BETWEEN $__timeFrom() AND $__timeTo()
ORDER BY tu.updated_at;


-- -----------------------------------------------------------------------------
-- 5. Number of tool changes per machine  (Bar chart / Stat panel)
-- -----------------------------------------------------------------------------
SELECT
    m.name                          AS machine,
    SUM(ms.tool_changes)            AS "Tool Changes"
FROM mes.machine_stats ms
JOIN mes.machines m USING (machine_id)
WHERE ms.recorded_at = (
    SELECT MAX(recorded_at)
    FROM mes.machine_stats
    WHERE machine_id = ms.machine_id
)
GROUP BY m.name
ORDER BY m.name;


-- -----------------------------------------------------------------------------
-- 6. Total pieces processed per machine  (Bar chart / Stat panel)
-- -----------------------------------------------------------------------------
SELECT
    m.name                          AS machine,
    ms.pieces_total                 AS "Total Pieces"
FROM mes.machine_stats ms
JOIN mes.machines m USING (machine_id)
WHERE ms.recorded_at = (
    SELECT MAX(recorded_at)
    FROM mes.machine_stats
    WHERE machine_id = ms.machine_id
)
ORDER BY m.name;


-- -----------------------------------------------------------------------------
-- 7. Pieces processed per machine per type  (Bar chart panel, grouped)
-- -----------------------------------------------------------------------------
SELECT
    m.name                          AS machine,
    pl.piece_type                   AS piece_type,
    COUNT(*)                        AS "Pieces"
FROM mes.production_log pl
JOIN mes.machines m USING (machine_id)
WHERE pl.status = 'completed'
GROUP BY m.name, pl.piece_type
ORDER BY m.name, pl.piece_type;


-- -----------------------------------------------------------------------------
-- 8. Pieces per type over time — time series  (Time series panel)
--    Shows production throughput by piece type
-- -----------------------------------------------------------------------------
SELECT
    DATE_TRUNC('minute', pl.finished_at) AS "time",
    pl.piece_type                         AS metric,
    COUNT(*)                              AS "Pieces Completed"
FROM mes.production_log pl
WHERE pl.status = 'completed'
  AND pl.finished_at BETWEEN $__timeFrom() AND $__timeTo()
GROUP BY DATE_TRUNC('minute', pl.finished_at), pl.piece_type
ORDER BY 1, 2;


-- -----------------------------------------------------------------------------
-- 9. Failed vs completed pieces per machine  (Bar chart panel)
-- -----------------------------------------------------------------------------
SELECT
    m.name                          AS machine,
    pl.status                       AS status,
    COUNT(*)                        AS "Count"
FROM mes.production_log pl
JOIN mes.machines m USING (machine_id)
GROUP BY m.name, pl.status
ORDER BY m.name, pl.status;


-- -----------------------------------------------------------------------------
-- 10. Currently mounted tool per machine + mode  (Table panel — live status)
-- -----------------------------------------------------------------------------
SELECT
    m.name                          AS machine,
    m.cell                          AS cell,
    COALESCE(m.current_tool, '—')  AS "Mounted Tool",
    m.mode                          AS mode,
    m.updated_at                    AS "Last Updated"
FROM mes.machines m
ORDER BY m.cell, m.name;


-- =============================================================================
-- UNLOADED WORK-PIECES
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 11. Total unloaded pieces per dock  (Stat / Bar Gauge panel)
-- -----------------------------------------------------------------------------
SELECT
    'Dock ' || dock_id              AS dock,
    SUM(count)                      AS "Total Unloaded"
FROM mes.unload_stats
GROUP BY dock_id
ORDER BY dock_id;


-- -----------------------------------------------------------------------------
-- 12. Unloaded pieces per dock per type  (Bar chart panel, grouped)
-- -----------------------------------------------------------------------------
SELECT
    'Dock ' || dock_id              AS dock,
    piece_type,
    count                           AS "Count"
FROM mes.unload_stats
ORDER BY dock_id, piece_type;


-- -----------------------------------------------------------------------------
-- 13. Unloaded pieces per type (all docks combined)  (Pie chart panel)
-- -----------------------------------------------------------------------------
SELECT
    piece_type,
    SUM(count)                      AS "Total"
FROM mes.unload_stats
GROUP BY piece_type
ORDER BY "Total" DESC;


-- -----------------------------------------------------------------------------
-- 14. Unload history over time — time series  (Time series panel)
--    updated_at reflects last time a dock/type counter was bumped
-- -----------------------------------------------------------------------------
SELECT
    updated_at                           AS "time",
    'Dock ' || dock_id || ' ' || piece_type AS metric,
    count                                AS "Count"
FROM mes.unload_stats
WHERE updated_at BETWEEN $__timeFrom() AND $__timeTo()
ORDER BY updated_at;


-- =============================================================================
-- BONUS — ORDER STATUS OVERVIEW  (useful alongside the above)
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 15. Order status summary  (Pie chart / Bar chart panel)
-- -----------------------------------------------------------------------------
SELECT
    status,
    COUNT(*)                        AS "Orders"
FROM mes.orders
GROUP BY status
ORDER BY status;


-- -----------------------------------------------------------------------------
-- 16. Pending orders by deadline  (Table panel — scheduler view)
-- -----------------------------------------------------------------------------
SELECT
    o.order_id,
    c.name                          AS client,
    o.type                          AS piece_type,
    o.quantity,
    o."DDate"                       AS deadline_days,
    o.penalty                       AS "Penalty (€/day)",
    COALESCE(o.priority::text, '—') AS priority,
    o.status
FROM mes.orders o
JOIN mes.client_orders co USING (client_order_id)
JOIN mes.clients c USING (client_id)
WHERE o.status IN ('PENDING', 'IN_PROGRESS')
ORDER BY
    o.priority ASC NULLS LAST,
    o."DDate"  ASC,
    o.penalty  DESC;