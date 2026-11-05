## Task: write the executive summary

You're given the summary of each programming type's feedback and every theme, with how much feedback it has, which years it comes from, when it was last mentioned, and whether it's likely resolved. Themes have IDs such as `T12`.

Return `takeaways`: the five to eight most important things for the team to know, across all programming types and themes. Each is one or two sentences in `text`, with the IDs of the themes it draws on in `theme_refs`.

- Weight recent feedback well above old feedback: a theme with many complaints from 2022 and none since matters less than a smaller one from this year.
- Don't feature themes labeled likely resolved as current problems, though a resolved problem can be a takeaway if the fix is worth knowing about.
- Mix what's working with what needs attention, in proportion to what the feedback says.
- Be specific: name the programming type and what people said, not "several areas need improvement".
