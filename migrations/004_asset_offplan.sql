-- Marking a loom as deliberately not in production.
--
-- The feed cannot tell "no order, no yarn, planned maintenance" from "broken": the loom
-- is still powered and still reporting rpm 0 either way, and nothing on the floor records
-- which it is. Over one observed day that gap accounted for most of the downtime on one
-- loom and three of the four hour-plus stops on the whole fleet — so every downtime
-- number the system produces today is a mixture of two things nobody separated.
--
-- Two costs, both real. Nobody can act on a report that counts planned idle as fault
-- time. And a deliberately idle loom generates a prompt, a re-prompt and an escalation
-- to the shift in-charge about a machine nobody intended to run, which is how people
-- learn to ignore the system.
--
-- Set once and it covers hours. Deliberately NOT a per-incident question: asking each
-- time is the added work this is supposed to remove. `until_at` is mandatory so a loom
-- cannot be silently parked forever — a forgotten flag would hide a genuine fault.

CREATE TABLE IF NOT EXISTS asset_offplan (
  asset_id   VARCHAR(128) NOT NULL,
  reason     VARCHAR(64)  NOT NULL,      -- no_order | no_yarn | maintenance | other
  note       VARCHAR(255) NULL,
  from_at    CHAR(25)     NOT NULL,
  until_at   CHAR(25)     NOT NULL,      -- mandatory: no open-ended parking
  set_by     VARCHAR(64)  NULL,
  PRIMARY KEY (asset_id),
  CONSTRAINT fk_offplan_asset FOREIGN KEY (asset_id) REFERENCES assets(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
