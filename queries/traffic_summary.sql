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

summary AS (
  SELECT

    COUNT(DISTINCT IF(
      session_date BETWEEN p.start_date AND p.end_date,
      session_uid,
      NULL
    )) AS current_total_sessions,

    COUNT(DISTINCT IF(
      session_date BETWEEN p.start_date AND p.end_date,
      user_pseudo_id,
      NULL
    )) AS current_total_users,

    COUNT(DISTINCT IF(
      session_date BETWEEN p.start_date AND p.end_date
      AND user_label = 'new_user',
      user_pseudo_id,
      NULL
    )) AS current_new_users,

    COUNT(DISTINCT IF(
      session_date BETWEEN p.start_date AND p.end_date
      AND user_label = 'returning_user',
      user_pseudo_id,
      NULL
    )) AS current_returning_users,

    COUNT(DISTINCT IF(
      session_date BETWEEN p.previous_start_date AND p.previous_end_date,
      session_uid,
      NULL
    )) AS previous_total_sessions,

    COUNT(DISTINCT IF(
      session_date BETWEEN p.previous_start_date AND p.previous_end_date,
      user_pseudo_id,
      NULL
    )) AS previous_total_users,

    COUNT(DISTINCT IF(
      session_date BETWEEN p.previous_start_date AND p.previous_end_date
      AND user_label = 'new_user',
      user_pseudo_id,
      NULL
    )) AS previous_new_users,

    COUNT(DISTINCT IF(
      session_date BETWEEN p.previous_start_date AND p.previous_end_date
      AND user_label = 'returning_user',
      user_pseudo_id,
      NULL
    )) AS previous_returning_users

  FROM `{project_id}.{dataset_id}.mar_ga_sessions`
  CROSS JOIN periods p

  WHERE session_date
    BETWEEN p.previous_start_date AND p.end_date
)

SELECT
  p.start_date,
  p.end_date,
  p.previous_start_date,
  p.previous_end_date,

  STRUCT(
    s.current_total_sessions AS total_sessions,
    s.current_total_users AS total_users,
    s.current_new_users AS new_users,
    s.current_returning_users AS returning_users
  ) AS current_period,

  STRUCT(
    s.previous_total_sessions AS total_sessions,
    s.previous_total_users AS total_users,
    s.previous_new_users AS new_users,
    s.previous_returning_users AS returning_users
  ) AS previous_period,

  STRUCT(
    ROUND(
      SAFE_DIVIDE(
        s.current_total_sessions - s.previous_total_sessions,
        s.previous_total_sessions
      ) * 100,
      2
    ) AS total_sessions,

    ROUND(
      SAFE_DIVIDE(
        s.current_total_users - s.previous_total_users,
        s.previous_total_users
      ) * 100,
      2
    ) AS total_users,

    ROUND(
      SAFE_DIVIDE(
        s.current_new_users - s.previous_new_users,
        s.previous_new_users
      ) * 100,
      2
    ) AS new_users,

    ROUND(
      SAFE_DIVIDE(
        s.current_returning_users - s.previous_returning_users,
        s.previous_returning_users
      ) * 100,
      2
    ) AS returning_users
  ) AS change_pct

FROM summary s
CROSS JOIN periods p;