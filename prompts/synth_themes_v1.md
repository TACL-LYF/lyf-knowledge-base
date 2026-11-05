## Task: group feedback that isn't about one programming type

You're given every feedback record that isn't about a single programming type: cross-cutting concerns such as communication, staff wellness, safety, volunteers, or apps. Group them into themes the records suggest.

- `themes`: group **every record** into themes. A theme is a specific, recurring subject ("Schedule changes announced too late"), not a broad category ("Communication"). Each has a short `name` (Title Case, a few words), a one- or two-sentence `description` of what people said, and the `record_ids` of all its records. One record can belong to two themes if it clearly makes two points. You may be given theme names from the previous run: reuse a name when it still fits, so the doc stays stable between runs. Set `likely_resolved` to `null` unless the records themselves show a change that addressed the theme after which the feedback stopped (then `change` is one sentence and `record_ids` the records showing the change).
- `requests`: the feature requests and bug reports (category `feature_request` or `bug`), merged where several records ask for the same thing, each with `category`, one-sentence `text`, `record_ids`, and `likely_resolved` (usually `null`).
- `open_questions`: questions nobody answered, or feedback too unclear to act on, each as one sentence with its records. Usually empty.
