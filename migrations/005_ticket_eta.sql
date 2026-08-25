-- The fixer's own time estimate, and whether they kept it.
--
-- Closing the loop: when a reason is set, the owning team is asked "how many hours will
-- the fix take?" Their answer snoozes all chasing for exactly that long, and if the
-- machine is still stopped when the estimate expires the cycle restarts and the miss is
-- COUNTED. eta_misses is the enforcement metric: "biggest defaulter" falls straight out
-- of promises made versus kept, each recorded at the moment it happened.
--
-- eta_hours: the current promise. eta_due_at: when it expires. eta_by: the person id
-- who made it. No inline comments after statements — the migration splitter takes them
-- as part of the next statement.

ALTER TABLE tickets ADD COLUMN eta_hours INT NULL;

ALTER TABLE tickets ADD COLUMN eta_due_at CHAR(25) NULL;

ALTER TABLE tickets ADD COLUMN eta_by VARCHAR(64) NULL;

ALTER TABLE tickets ADD COLUMN eta_misses INT NOT NULL DEFAULT 0;
