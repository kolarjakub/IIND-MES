-- =============================================================================
-- GRAFANA SQL QUERIES — MES Dashboard (Updated with tool_times & pieces_by_type)
-- =============================================================================
-- PostgreSQL data source, schema: mes
-- New schema includes: tool_times FLOAT[], pieces_by_type INT[] in machine_stats
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


-- =============================================================================
-- TOOL TIMES (from machine_stats.tool_times array)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 3a. Total operating time per tool (unnested from machine_stats)  (Bar chart)
--     Rows: machine+tool   Value: cumulative time
-- Mapping: tool_times[1]=T1, tool_times[2]=T2, tool_times[3]=T3 (1-indexed)
-- -----------------------------------------------------------------------------
SELECT
    m.name                                                   AS machine,
    CASE t.tool_idx
        WHEN 1 THEN 'T1' WHEN 2 THEN 'T2' WHEN 3 THEN 'T3'
    END                                                      AS tool,
    ROUND((t.tool_time)::numeric, 1)                         AS "Total Time (s)"
FROM mes.machines m
JOIN mes.machine_stats ms
    ON ms.machine_id = m.machine_id
    AND ms.recorded_at = (
        SELECT MAX(recorded_at)
        FROM mes.machine_stats
        WHERE machine_id = m.machine_id
    )
CROSS JOIN LATERAL (
    SELECT 1 AS tool_idx, ms.tool_times[1] AS tool_time
    UNION ALL
    SELECT 2, ms.tool_times[2]
    UNION ALL
    SELECT 3, ms.tool_times[3]
) t
WHERE t.tool_time > 0
ORDER BY m.name, t.tool_idx;


-- -----------------------------------------------------------------------------
-- 3b. Tool usage over time — time series per tool  (Time series panel)
--     Shows how cumulative tool time grows across simulation
--     Grafana: set "Time column" = recorded_at, group by machine+tool
-- -----------------------------------------------------------------------------
SELECT
    ms.recorded_at                                                   AS "time",
    m.name || '/' || CASE t.tool_idx
        WHEN 1 THEN 'T1' WHEN 2 THEN 'T2' WHEN 3 THEN 'T3'
    END                                                              AS metric,
    ROUND((t.tool_time)::numeric, 1)                                AS "Cumulative Time (s)"
FROM mes.machine_stats ms
JOIN mes.machines m USING (machine_id)
CROSS JOIN LATERAL (
    SELECT 1 AS tool_idx, ms.tool_times[1] AS tool_time
    UNION ALL
    SELECT 2, ms.tool_times[2]
    UNION ALL
    SELECT 3, ms.tool_times[3]
) t
WHERE ms.recorded_at BETWEEN $__timeFrom() AND $__timeTo()
  AND t.tool_time > 0
ORDER BY ms.recorded_at, m.name, t.tool_idx;


-- =============================================================================
-- NUMBER OF TOOL CHANGES PER MACHINE
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 4. Total tool changes per machine (latest snapshot)  (Bar chart / Stat panel)
-- -----------------------------------------------------------------------------
SELECT
    m.name                          AS machine,
    ms.tool_changes                 AS "Tool Changes"
FROM mes.machine_stats ms
JOIN mes.machines m USING (machine_id)
WHERE ms.recorded_at = (
    SELECT MAX(recorded_at)
    FROM mes.machine_stats
    WHERE machine_id = ms.machine_id
)
ORDER BY m.name;


-- =============================================================================
-- TOTAL PIECES PROCESSED (from machine_stats.pieces_total)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 5. Total pieces processed per machine (latest)  (Bar chart / Stat panel)
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


-- =============================================================================
-- PIECES BY TYPE (from machine_stats.pieces_by_type array)
-- =============================================================================

-- Mapping: pieces_by_type indices (0-based, 12 types):
--   0=RtopW(3), 1=StopW(4), 2=LegW(5), 3=RtopM(6), 4=StopM(7), 5=LegM(8),
--   6=RWW(9), 7=SWW(10), 8=RWM(11), 9=SWM(12), 10=RMM(13), 11=SMM(14)

-- -----------------------------------------------------------------------------
-- 6. Pieces processed per machine per type (from snapshot)  (Bar chart, grouped)
--    Uses pieces_by_type array from latest machine_stats
-- -----------------------------------------------------------------------------
SELECT
    m.name                                           AS machine,
    CASE t.type_idx
        WHEN 0 THEN 'RtopW'   WHEN 1 THEN 'StopW'   WHEN 2 THEN 'LegW'
        WHEN 3 THEN 'RtopM'   WHEN 4 THEN 'StopM'   WHEN 5 THEN 'LegM'
        WHEN 6 THEN 'RWW'     WHEN 7 THEN 'SWW'     WHEN 8 THEN 'RWM'
        WHEN 9 THEN 'SWM'     WHEN 10 THEN 'RMM'    WHEN 11 THEN 'SMM'
    END                                              AS piece_type,
    (ms.pieces_by_type[t.type_idx + 1])::INT        AS "Pieces"
FROM mes.machines m
JOIN mes.machine_stats ms
    ON ms.machine_id = m.machine_id
    AND ms.recorded_at = (
        SELECT MAX(recorded_at)
        FROM mes.machine_stats
        WHERE machine_id = m.machine_id
    )
CROSS JOIN (
    SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL
    SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL
    SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL
    SELECT 9 UNION ALL SELECT 10 UNION ALL SELECT 11
) t(type_idx)
WHERE (ms.pieces_by_type[t.type_idx + 1])::INT > 0
ORDER BY m.name, t.type_idx;


-- -----------------------------------------------------------------------------
-- 7. Pieces per type over time — time series from snapshots  (Time series panel)
--    Shows production throughput by piece type (from machine_stats snapshots)
-- -----------------------------------------------------------------------------
SELECT
    ms.recorded_at                                           AS "time",
    CASE t.type_idx
        WHEN 0 THEN 'RtopW'   WHEN 1 THEN 'StopW'   WHEN 2 THEN 'LegW'
        WHEN 3 THEN 'RtopM'   WHEN 4 THEN 'StopM'   WHEN 5 THEN 'LegM'
        WHEN 6 THEN 'RWW'     WHEN 7 THEN 'SWW'     WHEN 8 THEN 'RWM'
        WHEN 9 THEN 'SWM'     WHEN 10 THEN 'RMM'    WHEN 11 THEN 'SMM'
    END                                                      AS metric,
    (ms.pieces_by_type[t.type_idx + 1])::INT                AS "Pieces Processed"
FROM mes.machine_stats ms
JOIN mes.machines m USING (machine_id)
CROSS JOIN (
    SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL
    SELECT 3 UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL
    SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL
    SELECT 9 UNION ALL SELECT 10 UNION ALL SELECT 11
) t(type_idx)
WHERE ms.recorded_at BETWEEN $__timeFrom() AND $__timeTo()
  AND (ms.pieces_by_type[t.type_idx + 1])::INT > 0
ORDER BY ms.recorded_at, m.name, t.type_idx;


-- =============================================================================
-- PRODUCTION LOG QUERIES (detailed per-piece tracking)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 8. Pieces completed per type (aggregate from production_log)  (Pie chart)
-- -----------------------------------------------------------------------------
SELECT
    pl.piece_type                   AS piece_type,
    COUNT(*)                        AS "Total"
FROM mes.production_log pl
WHERE pl.status = 'completed'
GROUP BY pl.piece_type
ORDER BY "Total" DESC;


-- =============================================================================
-- UNLOADED WORK-PIECES
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 9. Total unloaded pieces per dock  (Stat / Bar Gauge panel)
-- -----------------------------------------------------------------------------
SELECT
    'Dock ' || dock_id              AS dock,
    SUM(count)                      AS "Total Unloaded"
FROM mes.unload_stats
GROUP BY dock_id
ORDER BY dock_id;


-- -----------------------------------------------------------------------------
-- 10. Unloaded pieces per dock per type  (Bar chart panel, grouped)
-- -----------------------------------------------------------------------------
SELECT
    'Dock ' || dock_id              AS dock,
    piece_type,
    count                           AS "Count"
FROM mes.unload_stats
ORDER BY dock_id, piece_type;


-- -----------------------------------------------------------------------------
-- 11. Unloaded pieces per type (all docks combined)  (Pie chart panel)
-- -----------------------------------------------------------------------------
SELECT
    piece_type,
    SUM(count)                      AS "Total"
FROM mes.unload_stats
GROUP BY piece_type
ORDER BY "Total" DESC;


-- =============================================================================
-- LIVE MACHINE STATUS
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 12. Currently mounted tool per machine + mode  (Table panel)
-- -----------------------------------------------------------------------------
SELECT
    m.name                          AS machine,
    m.cell                          AS cell,
    COALESCE(m.current_tool, '—')   AS "Mounted Tool",
    m.mode                          AS mode,
    m.updated_at                    AS "Last Updated"
FROM mes.machines m
ORDER BY m.cell, m.name;
