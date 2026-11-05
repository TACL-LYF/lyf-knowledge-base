## Task: write one programming type's sections

You're given every record about one programming type: its program facts and its feedback. Write both its section in Consolidated Feedback and its section in Programs.

### Consolidated Feedback

- `summary`: two or three sentences on what people say about this programming type overall, weighted toward recent feedback.
- `working` and `improve`: the main points on what's working and what to improve, each a short sentence with the records behind it. Usually two to five of each; fewer if the feedback is thin.
- `themes`: group **every feedback record** into themes. A theme is a specific, recurring subject ("Food ran out early", "Too loud near the stage"), not a category ("Logistics"). Each theme has a short `name` (Title Case, a few words), a one- or two-sentence `description` of what people said, and the `record_ids` of all its feedback records. One record can belong to two themes if it clearly makes two points. A single record can be its own theme if it doesn't fit elsewhere.
  - You may be given theme names from the previous run. Reuse a name when it still describes the theme, so the doc stays stable between runs; rename, split, merge, or drop themes when the records call for it.
  - `likely_resolved`: when a theme's feedback stops after a change to the program that addresses it (for example, "sold out early" complaints in 2023, a 2024 move to three nights, and no complaints since), set `change` to one sentence describing that change and `record_ids` to the program fact records showing the change. Otherwise `null`. Only label a theme likely resolved if all its feedback is dated before or during the period of the change.
- `requests`: the feature requests and bug reports among the feedback (category `feature_request` or `bug`), merged where several records ask for the same thing, each with `category`, a one-sentence `text`, its `record_ids`, and `likely_resolved` as for themes.
- `open_questions`: feedback that is a question nobody answered, or that's too unclear to act on (often category `confusion`), each as one sentence with its records. Usually empty.

### Programs

Only use program facts here (and feedback in `feedback_by_period`).

- `description`: what this programming type is, who it's for, and how it runs **now**, taken from the most recent facts. Two to six short points, each with the records behind it. If sources about the latest period disagree, say so in the point and cite both.
- `history`: a timeline, oldest first. Each entry has a `period` (as precise as the records allow, such as "2023" or "2024-09"; add "about" before an approximate date), a one-sentence `text`, `basis`, and `record_ids`:
  - `stated`: a source states the change or event (a `history` fact).
  - `found`: no source states the change, but `description` facts from different periods describe different setups (for example, a 2023 plan says "one night, courtyard" and a 2024 plan says "three nights, north lot"). Say what changed from what to what, and cite the records on **both** sides.
  - `conflict`: sources about the same period disagree. Say what each says and cite both.
  Include the first known setup as an entry too, so the timeline starts somewhere. Don't list every fact: only changes, one-time events, and the setups between them.
- `guidance`: advice for whoever runs this next (from `guidance` facts): how to lead it, lessons learned, what to prepare, what to avoid. Merge advice that says the same thing. Order by usefulness.
- `feedback_by_period`: for each distinct setup in the history (each period between changes), two or three sentences on what the feedback about that period said, citing the feedback records. For example, what people said about the one-night setup in 2023 compared with the three-night setup in 2024. Skip periods with no feedback. If the program never changed, one entry covering everything is enough.

If the type is marked **per-session**, the records you're given are about the type in general (sessions are handled separately): leave `feedback_by_period` empty, and keep `description` and `history` about the type in general, not any one session's topic.
