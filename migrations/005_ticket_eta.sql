-- The fixer's own time estimate, and whether they kept it.
--
-- Closing the loop: when a reason is set, the owning team is asked "how many hours will
-- the fix take?" Their answer snoozes all chasing for exactly that long — the system
-- stops nagging somebody who has committed to a time — and if the machine is still
-- stopped when the estimate expires, the cycle starts again and the miss is COUNTED.
--
-- eta_misses is the enforcement metric. "Who is the biggest defaulter" is unanswerable
-- from good intentions; it falls straight out of promises made versus kept, recorded at
-- the moment each happens.

ALTER TABLE tickets ADD COLUMN eta_hours INT NULL;          -- the current promise
ALTER TABLE tickets ADD COLUMN eta_due_at CHAR(25) NULL;    -- when it expires
ALTER TABLE tickets ADD COLUMN eta_by VARCHAR(64) NULL;     -- who promised (person id)
ALTER TABLE tickets ADD COLUMN eta_misses INT NOT NULL DEFAULT 0;
