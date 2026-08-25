-- Runtime-editable configuration.
--
-- The YAML files stay the base: git-tracked, heavily commented, and the thing you read
-- to understand the system. This table holds the DELTA applied on top, written by the
-- config API at runtime.
--
-- Why an overlay rather than rewriting the YAML in place:
--   * A YAML round-trip destroys comments, and the comments in reasons.yaml and
--     source.yaml are load-bearing documentation — why the RPM threshold is 40, why
--     fleet_fraction is 1.0 at four looms. Machine-writing those files would silently
--     delete the reasoning behind every number.
--   * Reverting a bad 3am edit becomes DELETE of one row, not a git operation on a
--     server.
--   * State that must survive a restart already lives in MySQL. Config is now the same
--     kind of thing as a pending timer.
--
-- One row per scope, holding the whole merged document as JSON. Effective config is
-- the YAML with this applied as an RFC 7386 merge patch, validated as a unit before
-- it is ever committed — so the boot-time "fail loud" guarantee becomes "reject the
-- write", never "accept a config that routes nothing to nobody".

CREATE TABLE IF NOT EXISTS config_overrides (
  scope      VARCHAR(32) NOT NULL,          -- reasons | routing | escalation
  patch      MEDIUMTEXT  NOT NULL,          -- JSON merge patch over the YAML
  updated_at CHAR(25)    NOT NULL,
  updated_by VARCHAR(64) NULL,
  PRIMARY KEY (scope)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
