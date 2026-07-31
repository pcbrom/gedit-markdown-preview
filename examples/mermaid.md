# Mermaid diagram test

A document to check diagram rendering in the gedit preview. The blocks below
should become figures, not raw text.

## Flowchart

```mermaid
flowchart TD
    A[.md document] --> B{Has a diagram?}
    B -->|yes| C[Load mermaid.js]
    B -->|no| D[Markdown only]
    C --> E[Render figure]
    D --> E
    E --> F[Preview in gedit]
```

## Sequence diagram

```mermaid
sequenceDiagram
    participant U as User
    participant G as gedit
    participant P as pandoc
    participant W as WebView
    U->>G: edit the .md
    G->>P: send the text
    P-->>G: HTML with <pre class="mermaid">
    G->>W: load the page
    W->>W: mermaid.run draws
    W-->>U: figure on screen
```

## Pie chart

```mermaid
pie title Time per step
    "Editing" : 45
    "Rendering" : 30
    "Reading" : 25
```

## Living together

The preview mixes everything: a diagram above, math here, $a^2 + b^2 = c^2$, and
a table:

| Kind | Syntax | Renders as |
|---|---|---|
| Markdown | `**x**` | bold |
| Math | `$x^2$` | MathML |
| Mermaid | `mermaid` block | figure |
