/** Plain names for the rule ids the Python checks return. Neutral, so they read
 *  right next to a pass as well as a block. */
export const RULE_NAMES: Record<string, string> = {
  calling_hours: 'Calling hours',
  call_frequency: 'Call limit (7 in 7 days)',
  tcpa_consent: 'Phone consent',
  cease: 'Stop requests',
  dnc: 'Do-not-call list',
  attorney: 'Lawyer',
  bankruptcy: 'Bankruptcy',
  deceased: 'Deceased',
  archived: 'Archived',
  precall: 'Missing data',
}

export const ruleName = (id: string) => RULE_NAMES[id] ?? id
