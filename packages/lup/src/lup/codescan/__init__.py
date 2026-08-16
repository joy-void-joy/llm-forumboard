"""Source scanning for development tooling: review notes and rule families.

`common` holds the scanning core every scanner shares — comment-column
tokenization, docstring detection, ignore-directive matching, file-level
opt-out, and the line cursor. `markers` lists `# lup:` review notes;
`antipatterns` audits forbidden code shapes; `boundaries` keeps native
imports, native wire spellings, and kernel imports inside their sanctioned
homes; `capabilities` enforces the capability-ABC composition architecture
project-wide.
`registry` indexes every rule family with its definition site for the generated
rule reference, while the common scanner also provides token-masked line
projections for rules that inspect code rather than prose.
"""
