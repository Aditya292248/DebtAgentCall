-- Made-up debtors for demos and tests. None of these people are real.
-- 555-01xx numbers are reserved for fiction. Nothing loads this file by itself:
-- `uv run seed-demo` loads it into DATABASE_URL (and refuses a database that holds
-- real people), and the test session loads it into its own `_test` database.
--
-- Each debtor from Casey Morgan on exists to trip one pre-call check. The call
-- history is dated around 2026-11-05, the fixed time `uv run demo part-07` uses.

insert into public.debtor (id, first_name, last_name, dob, address, zip, state) values
    ('00000000-0000-4000-8000-000000000001', 'Sam', 'Doe', '1985-04-12',
     '100 Example St, Los Angeles', '90012', 'CA'),
    ('00000000-0000-4000-8000-000000000002', 'Alex', 'Rivera', '1990-09-30',
     '200 Sample Ave, New York', '10001', 'NY'),
    ('00000000-0000-4000-8000-000000000003', 'Jordan', 'Lee', '1978-01-05',
     '300 Placeholder Rd, Chicago', '60601', 'IL'),
    ('00000000-0000-4000-8000-000000000004', 'Casey', 'Morgan', '1982-07-19',
     '400 Mock Ln, Boston', '02108', 'MA'),
    ('00000000-0000-4000-8000-000000000005', 'Riley', 'Chen', '1995-02-08',
     '500 Dummy Dr, Atlanta', '30303', 'GA'),
    ('00000000-0000-4000-8000-000000000006', 'Taylor', 'Brooks', '1988-11-23',
     '600 Fake Blvd, Miami', '33101', 'FL'),
    ('00000000-0000-4000-8000-000000000007', 'Morgan', 'Patel', '1975-05-14',
     '700 Test Ct, Dallas', '75201', 'TX'),
    ('00000000-0000-4000-8000-000000000008', 'Parker', 'Reed', '1992-03-27',
     '800 Stand-in Way, Denver', '80202', 'CO'),
    ('00000000-0000-4000-8000-000000000009', 'Avery', 'Ortiz', '1986-08-02',
     '900 Pretend Pl, Phoenix', '85004', 'AZ');

-- Alex lives in New York but kept a San Francisco (415) number.
-- Riley took back their consent; Taylor never gave any.
insert into public.phone (debtor_id, e164, line_type, consent_source, consent_at, revoked_at) values
    ('00000000-0000-4000-8000-000000000001', '+12135550101', 'cell',
     'account application', '2025-01-15 10:00:00+00', null),
    ('00000000-0000-4000-8000-000000000002', '+14155550102', 'cell',
     'account application', '2025-03-02 16:30:00+00', null),
    ('00000000-0000-4000-8000-000000000003', '+13125550103', 'landline',
     'account application', '2025-06-20 14:00:00+00', null),
    ('00000000-0000-4000-8000-000000000004', '+16175550104', 'cell',
     'account application', '2025-02-11 12:00:00+00', null),
    ('00000000-0000-4000-8000-000000000005', '+14045550105', 'cell',
     'account application', '2025-04-01 09:00:00+00', '2026-10-28 18:20:00+00'),
    ('00000000-0000-4000-8000-000000000006', '+13055550106', 'cell',
     null, null, null),
    ('00000000-0000-4000-8000-000000000007', '+12145550107', 'cell',
     'account application', '2025-05-05 15:00:00+00', null),
    ('00000000-0000-4000-8000-000000000008', '+13035550108', 'cell',
     'account application', '2025-05-20 15:00:00+00', null),
    ('00000000-0000-4000-8000-000000000009', '+16025550109', 'landline',
     'account application', '2025-07-07 17:00:00+00', null);

insert into public.debt (debtor_id, account_number, creditor, balance_cents, status, opened_on) values
    ('00000000-0000-4000-8000-000000000001', 'ACC-1001', 'Northwind Furniture', 125000, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000002', 'ACC-1002', 'Bluebird Dental', 31045, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000003', 'ACC-1003', 'Maple Street Gym', 8999, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000004', 'ACC-1004', 'Harbor Light Utilities', 45210, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000005', 'ACC-1005', 'Peachtree Auto Repair', 72000, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000006', 'ACC-1006', 'Seabreeze Vet Clinic', 15075, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000007', 'ACC-1007', 'Lone Oak Hardware', 23400, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000008', 'ACC-1008', 'Summit Ski Rentals', 9800, 'active', '2026-01-15'),
    ('00000000-0000-4000-8000-000000000009', 'ACC-1009', 'Desert Bloom Florist', 6150, 'active', '2026-01-15');

-- Every call we placed, answered or not.
--   Sam:    one old conversation, long enough ago that it no longer counts.
--   Alex:   6 unanswered calls in the 7 days before 2026-11-05, so a 7th is allowed.
--   Jordan: a real conversation on 2026-11-02, so no calls until 2026-11-09.
--   Casey:  7 unanswered calls in the 7 days before 2026-11-05, so an 8th is blocked.
insert into public.call_attempt (debt_id, phone_id, placed_at, outcome, connected, conversation_with_debtor)
select d.id, p.id, a.placed_at::timestamptz, a.outcome, a.connected, a.conversation
from (values
    ('ACC-1001', '+12135550101', '2026-10-01 18:00:00+00', 'answered', true, true),
    ('ACC-1002', '+14155550102', '2026-10-20 17:00:00+00', 'no_answer', false, false),
    ('ACC-1002', '+14155550102', '2026-10-30 17:00:00+00', 'no_answer', false, false),
    ('ACC-1002', '+14155550102', '2026-10-31 17:00:00+00', 'voicemail', false, false),
    ('ACC-1002', '+14155550102', '2026-11-01 17:00:00+00', 'no_answer', false, false),
    ('ACC-1002', '+14155550102', '2026-11-02 17:00:00+00', 'busy', false, false),
    ('ACC-1002', '+14155550102', '2026-11-03 17:00:00+00', 'no_answer', false, false),
    ('ACC-1002', '+14155550102', '2026-11-04 17:00:00+00', 'voicemail', false, false),
    ('ACC-1003', '+13125550103', '2026-11-02 15:00:00+00', 'answered', true, true),
    ('ACC-1004', '+16175550104', '2026-10-30 15:00:00+00', 'no_answer', false, false),
    ('ACC-1004', '+16175550104', '2026-10-31 15:00:00+00', 'no_answer', false, false),
    ('ACC-1004', '+16175550104', '2026-11-01 15:00:00+00', 'voicemail', false, false),
    ('ACC-1004', '+16175550104', '2026-11-02 15:00:00+00', 'no_answer', false, false),
    ('ACC-1004', '+16175550104', '2026-11-03 15:00:00+00', 'busy', false, false),
    ('ACC-1004', '+16175550104', '2026-11-04 15:00:00+00', 'no_answer', false, false),
    ('ACC-1004', '+16175550104', '2026-11-05 14:00:00+00', 'no_answer', false, false)
) as a (account_number, e164, placed_at, outcome, connected, conversation)
join public.debt d on d.account_number = a.account_number
join public.phone p on p.e164 = a.e164;

-- Flags that stop calls. A null debt_id means the flag covers every debt of that debtor.
insert into public.flags (debtor_id, debt_id, kind, data, set_at, source)
select p.id, d.id, f.kind, f.data::jsonb, f.set_at::timestamptz, f.source
from (values
    ('00000000-0000-4000-8000-000000000007', 'ACC-1007', 'cease',
     '{"how": "letter"}', '2026-10-15 12:00:00+00', 'mail'),
    ('00000000-0000-4000-8000-000000000008', null, 'opt_out_phone',
     '{"e164": "+13035550108"}', '2026-10-21 16:00:00+00', 'call'),
    ('00000000-0000-4000-8000-000000000009', null, 'dnc',
     '{"e164": "+16025550109"}', '2026-09-30 00:00:00+00', 'internal dnc list')
) as f (debtor_id, account_number, kind, data, set_at, source)
join public.debtor p on p.id = f.debtor_id::uuid
left join public.debt d on d.account_number = f.account_number;
