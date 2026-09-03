-- Retiring a machine that is still wired up.
--
-- active=0 already meant "not in service", but it could not survive contact with the
-- feed: ensure_asset treats a machine sending data as proof it is back, and flips the
-- flag on again. That is right for a loom that went quiet and returned, and wrong for
-- one somebody has deliberately retired -- a decommissioned loom still on the network
-- came back within one poll, so the button that promised "permanently" lasted thirty
-- seconds.
--
-- A timestamp set only by a human is the difference. Reporting no longer overrides it,
-- and clearing it is what puts the machine back in service.

ALTER TABLE assets ADD COLUMN decommissioned_at CHAR(25) NULL;
