-- Seed: one DLT-registered utility template. The sms gate requires a
-- registered template row -- the agent NEVER improvises copy on a DLT channel
-- (TCCCPR: templates are pre-approved artefacts, promotional mixing
-- reclassifies the message).
INSERT IGNORE INTO dlt_templates (template_id, header, category, body, language, active)
VALUES (
    'UTIL_2291',
    'RZPREV',
    'utility',
    'Dear customer, your payment of Rs {amount} for {product} could not be processed. Complete it securely here: {link}. Reply STOP to opt out.',
    'en',
    TRUE
);
