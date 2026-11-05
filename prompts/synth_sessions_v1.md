## Task: write one year of a per-session programming type

Some programming types, such as workshops, run several sessions a year, each with its own content. You're given one year of one such type: the records about each named session, and feedback about a session of that year that doesn't say which.

- `sessions`: one entry per session listed, using its `name` exactly as given.
  - `covered`: what the session covered and how it ran (topic, activities, materials, format), as one to four short points from its program facts.
  - `guidance`: advice that came out of this session, for whoever runs a similar one.
  - `feedback_summary`: one to three sentences on what people said about this session, or `null` if there's no feedback on it.
  - `feedback_record_ids`: every feedback record about this session, including any from the unmatched list you're confident belong to it (matching dates are the strongest clue).
- `unmatched`: for the unmatched feedback you couldn't place, a one- or two-sentence `summary` (or `null` if there's none left) and its `record_ids`. Don't include records that are really about the type in general ("workshops always run long"); leave those out of every list, since they're covered elsewhere.
- `recurring`: points that come up in **two or more sessions** this year ("three workshops ran over time"), as one sentence each, with `kind` (`guidance` or `feedback`) and the records from each session it draws on.
