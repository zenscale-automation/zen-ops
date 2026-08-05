-- ops-core initial schema — MySQL / MariaDB (InnoDB, utf8mb4).
-- Applied automatically by app/db.py at boot, and also importable directly into
-- phpMyAdmin (see deploy/schema.mysql.sql for a prefix-applied copy).
--
-- Timestamps are stored as UTC ISO-8601 strings in CHAR(25) (e.g.
-- "2026-08-05T09:30:00+00:00"). Fixed width => lexicographic order == chronological,
-- so every `due_at <= now` comparison in the timer logic stays a plain string compare
-- and Rule 4 (UTC in storage, IST at the edges) holds without timezone-typed columns.
--
-- Deviations from the design doc's section 5 schema (all additive/minimal; see README):
--   * incidents.resolve_due_at  -- grace-window timer as a ROW (Rule 1), false-restart guard.
--   * escalations.ticket_id is NULLABLE and escalations.incident_id + escalations.action
--     were added so the pre-ticket "unknown"/ask_reason ladder can run against an incident.
--   * inbound_raw.matched_incident_id -- a Phase-1 reply answers a reason prompt on an incident.
--   * `condition` and `trigger` are reserved words in MySQL, kept as column names via backticks.

-- Every CREATE is IF NOT EXISTS so this file is RE-RUNNABLE. MySQL implicitly commits
-- each DDL statement, so a migration cannot be atomic: if one fails partway (or the
-- tables were imported by hand via phpMyAdmin), the schema_migrations row never gets
-- written and the next boot would re-run the file. Without IF NOT EXISTS that boot dies
-- on "Table 'assets' already exists" and the deployment is stuck with no way forward
-- short of dropping tables by hand.

CREATE TABLE IF NOT EXISTS assets (
  id          VARCHAR(128) NOT NULL,            -- "weaving:loom_23"
  department  VARCHAR(64)  NOT NULL,
  asset_ref   VARCHAR(64)  NOT NULL,            -- "loom_23"
  label       VARCHAR(255) NULL,
  active      TINYINT      NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  UNIQUE KEY uq_asset (department, asset_ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS incidents (
  id             BIGINT       NOT NULL AUTO_INCREMENT,
  asset_id       VARCHAR(128) NOT NULL,
  department     VARCHAR(64)  NOT NULL,
  opened_at      CHAR(25)     NOT NULL,         -- UTC ISO8601
  resolved_at    CHAR(25)     NULL,
  duration_s     INT          NULL,             -- computed on resolve
  shift          VARCHAR(8)   NULL,             -- A / B / C, resolved at open
  `condition`    VARCHAR(128) NULL,             -- from the source
  status         VARCHAR(16)  NOT NULL,         -- open | resolving | resolved
  resolve_due_at CHAR(25)     NULL,             -- grace-window timer (false-restart guard)
  PRIMARY KEY (id),
  KEY ix_inc_open  (status, department),
  KEY ix_inc_asset (asset_id, opened_at),
  CONSTRAINT fk_inc_asset FOREIGN KEY (asset_id) REFERENCES assets(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS incident_reasons (
  id          BIGINT      NOT NULL AUTO_INCREMENT,
  incident_id BIGINT      NOT NULL,
  code        VARCHAR(64) NOT NULL,             -- "weaving.electrical"
  subcode     VARCHAR(64) NULL,                 -- "float", "khonk"
  method      VARCHAR(16) NOT NULL,             -- panel | reply | auto | backfill
  actor       VARCHAR(64) NULL,                 -- person id, or "system"
  at          CHAR(25)    NOT NULL,
  PRIMARY KEY (id),
  KEY ix_reason_incident (incident_id),
  CONSTRAINT fk_reason_inc FOREIGN KEY (incident_id) REFERENCES incidents(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tickets (
  id                BIGINT      NOT NULL AUTO_INCREMENT,
  incident_id       BIGINT      NOT NULL,
  department        VARCHAR(64) NOT NULL,
  code              VARCHAR(64) NOT NULL,
  owner_role        VARCHAR(64) NOT NULL,
  opened_at         CHAR(25)    NOT NULL,
  first_notified_at CHAR(25)    NULL,
  attended_at       CHAR(25)    NULL,           -- presence at the asset (Phase 2; NULL in Phase 1)
  attended_by       VARCHAR(64) NULL,
  diagnosis         VARCHAR(255) NULL,          -- optional, from the fitter
  closed_at         CHAR(25)    NULL,
  close_reason      VARCHAR(32) NULL,           -- asset_resumed | manual | stale
  reopen_count      INT         NOT NULL DEFAULT 0,
  status            VARCHAR(16) NOT NULL,       -- open | attended | closed
  PRIMARY KEY (id),
  KEY ix_tkt_open      (status, department),
  KEY ix_tkt_incident  (incident_id),
  CONSTRAINT fk_tkt_inc FOREIGN KEY (incident_id) REFERENCES incidents(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS escalations (
  id           BIGINT      NOT NULL AUTO_INCREMENT,
  ticket_id    BIGINT      NULL,                -- set once a ticket exists
  incident_id  BIGINT      NULL,                -- set for the pre-ticket "unknown" ladder
  rung         INT         NOT NULL,
  notify_role  VARCHAR(64) NOT NULL,
  action       VARCHAR(32) NULL,                -- ask_reason | notify (NULL == notify)
  due_at       CHAR(25)    NOT NULL,            -- the timer, persisted
  fired_at     CHAR(25)    NULL,
  status       VARCHAR(16) NOT NULL,            -- pending | fired | cancelled
  `trigger`    VARCHAR(16) NULL,                -- timer | recurrence
  PRIMARY KEY (id),
  KEY ix_esc_due (status, due_at),
  CONSTRAINT fk_esc_tkt FOREIGN KEY (ticket_id)   REFERENCES tickets(id),
  CONSTRAINT fk_esc_inc FOREIGN KEY (incident_id) REFERENCES incidents(id),
  CONSTRAINT chk_esc_parent CHECK (ticket_id IS NOT NULL OR incident_id IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS outbox (
  id              BIGINT       NOT NULL AUTO_INCREMENT,
  channel         VARCHAR(16)  NOT NULL,        -- whatsapp | gchat | panel | log
  recipient       VARCHAR(191) NOT NULL,
  payload         JSON         NOT NULL,
  dedupe_key      VARCHAR(191) NOT NULL,
  attempts        INT          NOT NULL DEFAULT 0,
  next_try_at     CHAR(25)     NOT NULL,
  sent_at         CHAR(25)     NULL,
  provider_msg_id VARCHAR(128) NULL,
  status          VARCHAR(16)  NOT NULL,        -- queued | sent | failed
  PRIMARY KEY (id),
  UNIQUE KEY uq_dedupe (dedupe_key),
  KEY ix_out_due (status, next_try_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS inbound_raw (
  id                  BIGINT      NOT NULL AUTO_INCREMENT,
  channel             VARCHAR(16) NOT NULL,
  received_at         CHAR(25)    NOT NULL,
  body                TEXT        NOT NULL,      -- verbatim, before parsing
  matched_ticket_id   BIGINT      NULL,
  matched_incident_id BIGINT      NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS events (                            -- append-only, never updated, never deleted
  id         BIGINT      NOT NULL AUTO_INCREMENT,
  at         CHAR(25)    NOT NULL,
  department VARCHAR(64) NULL,
  entity     VARCHAR(16) NOT NULL,               -- incident | ticket | escalation
  entity_id  BIGINT      NOT NULL,
  kind       VARCHAR(24) NOT NULL,               -- opened | reason_set | notified | escalated | ...
  actor      VARCHAR(64) NULL,                   -- person id or "system"
  detail     JSON        NULL,
  PRIMARY KEY (id),
  KEY ix_ev_entity (entity, entity_id),
  KEY ix_ev_at     (at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
