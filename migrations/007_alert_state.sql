-- Somewhere to remember that an alert is already in flight.
--
-- The watchdog needs one fact across restarts: whether the send path was already down
-- last time it looked. Without it, a restart re-raises an alarm that is already raised,
-- and the recovery message ("sending works again") has nothing to compare against.
-- Deliberately a tiny key/value table rather than a column on something else — this is
-- the system's opinion about ITSELF, and it belongs nowhere near plant data.

CREATE TABLE IF NOT EXISTS alert_state (
  name       VARCHAR(64)  NOT NULL,
  value      VARCHAR(191) NULL,
  updated_at CHAR(25)     NOT NULL,
  PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
