-- Assigning one fault to one person, without rewriting the roster.
--
-- Routing is role-based on purpose: a role survives somebody leaving, and a 3am fault
-- reaches whoever is actually on nights. But a live incident sometimes needs to go to a
-- named person for reasons no roster can express -- he is already standing at that loom,
-- or the person on shift has just said it is not his job. Until now the only way to do
-- that was to edit the roster for everything, so nobody did it, and the fault sat with
-- whoever the rota named.
--
-- A person named here overrides the role for that escalation only. NULL means "use the
-- role", which is every row written by the ladders themselves.

ALTER TABLE escalations ADD COLUMN notify_person VARCHAR(64) NULL;

ALTER TABLE tickets ADD COLUMN owner_person VARCHAR(64) NULL;
