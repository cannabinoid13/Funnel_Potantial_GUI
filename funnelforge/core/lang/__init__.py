"""The Desmond M-expression language: lossless front end and semantic model.

The layers here are deliberately separate:

``lexer``
    text -> a token stream in which *every* character of the source, including
    whitespace, line endings and comments, belongs to exactly one token.
``cst``
    the token stream -> a concrete syntax tree of spans over the original text.
    Nothing is normalised, nothing is dropped, and anything the grammar cannot
    explain becomes an opaque node that still carries its exact span.
``sema``
    the tree -> symbols, value categories, a dependency graph and the set of
    contributions that actually reach the potential.
``registry``
    which functions a given Desmond release accepts.
``diagnostics``
    stable codes, severities and the seven independent validation layers.

The original text is the single source of truth.  Every structure in this
package is a *view* of it, addressed by character offsets, so an edit can be
applied as a patch to a span rather than by regenerating the file.
"""
