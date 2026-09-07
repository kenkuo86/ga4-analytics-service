WITH date_range AS (
  SELECT
    @start_date AS start_date,
    @end_date AS end_date,
    DATE_DIFF(@end_date, @start_date, DAY) + 1 AS period_days
),

periods AS (
  SELECT
    start_date,
    end_date,
    DATE_SUB(
      start_date,
      INTERVAL period_days DAY
    ) AS previous_start_date,
    DATE_SUB(
      start_date,
      INTERVAL 1 DAY
    ) AS previous_end_date
  FROM date_range
),

date_spine AS (
  SELECT
    day_index,
    DATE_ADD(p.start_date, INTERVAL day_index DAY) AS current_metric_date,
    DATE_ADD(p.previous_start_date, INTERVAL day_index DAY) AS previous_metric_date,
    p.start_date,
    p.end_date,
    p.previous_start_date,
    p.previous_end_date
  FROM periods p
  CROSS JOIN UNNEST(GENERATE_ARRAY(0, DATE_DIFF(p.end_date, p.start_date, DAY))) AS day_index
),

bounded_sessions AS (
  SELECT
    s.session_date,
    s.session_uid,
    s.user_pseudo_id,
    s.user_label
  FROM `{project_id}.{dataset_id}.mar_ga_sessions` s
  CROSS JOIN periods p
  WHERE s.session_date BETWEEN p.previous_start_date AND p.end_date
),

aligned_dates AS (
  SELECT
    'current' AS period_key,
    day_index,
    current_metric_date AS metric_date
  FROM date_spine

  UNION ALL

  SELECT
    'previous' AS period_key,
    day_index,
    previous_metric_date AS metric_date
  FROM date_spine
),

aggregates AS (
  SELECT
    d.period_key,
    d.day_index,
    d.metric_date,
    GROUPING(d.day_index) AS is_headline,

    COUNT(DISTINCT s.session_uid) AS total_sessions,
    COUNT(DISTINCT s.user_pseudo_id) AS total_users,

    COUNT(DISTINCT IF(
      s.user_label = 'new_user',
      s.user_pseudo_id,
      NULL
    )) AS new_users,

    COUNT(DISTINCT IF(
      s.user_label = 'returning_user',
      s.user_pseudo_id,
      NULL
  )) AS returning_users

  FROM aligned_dates d
  LEFT JOIN bounded_sessions s
    ON s.session_date = d.metric_date

  GROUP BY GROUPING SETS (
    (d.period_key),
    (d.period_key, d.day_index, d.metric_date)
  )
),

pivoted AS (
  SELECT
    day_index,
    is_headline,
    MAX(IF(period_key = 'current', metric_date, NULL)) AS current_metric_date,
    MAX(IF(period_key = 'previous', metric_date, NULL)) AS previous_metric_date,

    MAX(IF(period_key = 'current', total_sessions, NULL))
      AS current_total_sessions,
    MAX(IF(period_key = 'current', total_users, NULL))
      AS current_total_users,
    MAX(IF(period_key = 'current', new_users, NULL))
      AS current_new_users,
    MAX(IF(period_key = 'current', returning_users, NULL))
      AS current_returning_users,

    MAX(IF(period_key = 'previous', total_sessions, NULL))
      AS previous_total_sessions,
    MAX(IF(period_key = 'previous', total_users, NULL))
      AS previous_total_users,
    MAX(IF(period_key = 'previous', new_users, NULL))
      AS previous_new_users,
    MAX(IF(period_key = 'previous', returning_users, NULL))
      AS previous_returning_users

  FROM aggregates
  GROUP BY day_index, is_headline
)

SELECT
  p.start_date,
  p.end_date,
  p.previous_start_date,
  p.previous_end_date,

  STRUCT(
    MAX(IF(is_headline = 1, current_total_sessions, NULL)) AS total_sessions,
    MAX(IF(is_headline = 1, current_total_users, NULL)) AS total_users,
    MAX(IF(is_headline = 1, current_new_users, NULL)) AS new_users,
    MAX(IF(is_headline = 1, current_returning_users, NULL)) AS returning_users
  ) AS current_period,

  STRUCT(
    MAX(IF(is_headline = 1, previous_total_sessions, NULL)) AS total_sessions,
    MAX(IF(is_headline = 1, previous_total_users, NULL)) AS total_users,
    MAX(IF(is_headline = 1, previous_new_users, NULL)) AS new_users,
    MAX(IF(is_headline = 1, previous_returning_users, NULL)) AS returning_users
  ) AS previous_period,

  STRUCT(
    ROUND(
      SAFE_DIVIDE(
        MAX(IF(is_headline = 1, current_total_sessions, NULL))
          - MAX(IF(is_headline = 1, previous_total_sessions, NULL)),
        MAX(IF(is_headline = 1, previous_total_sessions, NULL))
      ) * 100,
      2
    ) AS total_sessions,

    ROUND(
      SAFE_DIVIDE(
        MAX(IF(is_headline = 1, current_total_users, NULL))
          - MAX(IF(is_headline = 1, previous_total_users, NULL)),
        MAX(IF(is_headline = 1, previous_total_users, NULL))
      ) * 100,
      2
    ) AS total_users,

    ROUND(
      SAFE_DIVIDE(
        MAX(IF(is_headline = 1, current_new_users, NULL))
          - MAX(IF(is_headline = 1, previous_new_users, NULL)),
        MAX(IF(is_headline = 1, previous_new_users, NULL))
      ) * 100,
      2
    ) AS new_users,

    ROUND(
      SAFE_DIVIDE(
        MAX(IF(is_headline = 1, current_returning_users, NULL))
          - MAX(IF(is_headline = 1, previous_returning_users, NULL)),
        MAX(IF(is_headline = 1, previous_returning_users, NULL))
      ) * 100,
      2
    ) AS returning_users
  ) AS change_pct,

  ARRAY_AGG(
    IF(
      is_headline = 0,
      STRUCT(
        day_index,
        current_metric_date AS date,
        previous_metric_date AS comparison_date,
        STRUCT(
          current_total_sessions AS total_sessions,
          current_total_users AS total_users,
          current_new_users AS new_users,
          current_returning_users AS returning_users
        ) AS `current`,
        STRUCT(
          previous_total_sessions AS total_sessions,
          previous_total_users AS total_users,
          previous_new_users AS new_users,
          previous_returning_users AS returning_users
        ) AS `previous`
      ),
      NULL
    ) IGNORE NULLS
    ORDER BY day_index
  ) AS daily_series

FROM periods p
CROSS JOIN pivoted
GROUP BY
  p.start_date,
  p.end_date,
  p.previous_start_date,
  p.previous_end_date;
