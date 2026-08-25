-- Record WHO sent an inbound message, not just what they sent.
--
-- WhatsApp bills business-initiated template messages but delivers free-form replies
-- free inside the 24-hour "customer service window" that opens whenever the person
-- messages us. To use that window we have to know, per recipient, when they last wrote
-- to us — which the original inbound_raw could not answer, since it stored only the
-- verbatim body.
--
-- It matters for correctness as much as cost: outside the window ONLY an approved
-- template may be sent, so a notifier that cannot tell which side of the window it is
-- on will eventually try to send free text and be rejected by Meta.
--
-- Stored as digits only (no '+', no spaces) because Meta reports `from` as
-- "919000000001" while routing.yaml carries "+91 90000 00001". Normalising on the way
-- in means the lookup never has to guess at a format.

ALTER TABLE inbound_raw ADD COLUMN sender VARCHAR(64) NULL;
ALTER TABLE inbound_raw ADD INDEX ix_inbound_sender (channel, sender, received_at);
