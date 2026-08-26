-- What actually happened to a message after the provider accepted it.
--
-- PickyAssist answers 100 the moment a message is QUEUED, and Meta's verdict arrives
-- later, out of band. Until now that verdict lived only in a web panel someone had to
-- go and read: a number with an unapproved display name dropped every message for an
-- hour while the outbox showed 'sent' and /health showed ok. These columns receive the
-- delivery reports their Event Webhook pushes, so a message that dies after acceptance
-- flips its row to 'failed' and the health metric that means "somebody was not called"
-- moves without a human reading anything.

ALTER TABLE outbox ADD COLUMN delivery_status VARCHAR(16) NULL;

ALTER TABLE outbox ADD COLUMN delivery_error VARCHAR(255) NULL;
