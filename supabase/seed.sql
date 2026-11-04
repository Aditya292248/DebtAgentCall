-- Made-up debtors for local development. None of these people are real.
-- 555-01xx numbers are reserved for fiction. `supabase db reset` loads this file.

insert into public.debtor (id, first_name, last_name, dob, address, zip, state) values
    ('00000000-0000-4000-8000-000000000001', 'Sam', 'Doe', '1985-04-12',
     '100 Example St, Los Angeles', '90012', 'CA'),
    ('00000000-0000-4000-8000-000000000002', 'Alex', 'Rivera', '1990-09-30',
     '200 Sample Ave, New York', '10001', 'NY'),
    ('00000000-0000-4000-8000-000000000003', 'Jordan', 'Lee', '1978-01-05',
     '300 Placeholder Rd, Chicago', '60601', 'IL');

-- Alex lives in New York but kept a San Francisco (415) number.
insert into public.phone (debtor_id, e164, line_type, consent_source, consent_at) values
    ('00000000-0000-4000-8000-000000000001', '+12135550101', 'cell',
     'account application', '2025-01-15 10:00:00+00'),
    ('00000000-0000-4000-8000-000000000002', '+14155550102', 'cell',
     'account application', '2025-03-02 16:30:00+00'),
    ('00000000-0000-4000-8000-000000000003', '+13125550103', 'landline',
     'account application', '2025-06-20 14:00:00+00');

insert into public.debt (debtor_id, account_number, creditor, balance_cents, status) values
    ('00000000-0000-4000-8000-000000000001', 'ACC-1001', 'Northwind Furniture', 125000, 'active'),
    ('00000000-0000-4000-8000-000000000002', 'ACC-1002', 'Bluebird Dental', 31045, 'active'),
    ('00000000-0000-4000-8000-000000000003', 'ACC-1003', 'Maple Street Gym', 8999, 'active');
