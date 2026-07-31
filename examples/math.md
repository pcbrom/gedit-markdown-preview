# Math rendering test

A document to check the gedit preview: inline math, display math, a table,
code, and lists.

## Inline

Euler's identity, $e^{i\pi} + 1 = 0$, combines five constants. The rest energy
is $E = mc^2$, and the golden ratio is $\varphi = \frac{1 + \sqrt{5}}{2}$.

## Display

The Gaussian integral:

$$\int_{-\infty}^{\infty} e^{-x^2}\,dx = \sqrt{\pi}$$

The Basel series:

$$\sum_{n=1}^{\infty} \frac{1}{n^2} = \frac{\pi^2}{6}$$

A $2 \times 2$ matrix and its determinant:

$$A = \begin{pmatrix} a & b \\ c & d \end{pmatrix}, \qquad \det A = ad - bc$$

Bayes' theorem:

$$P(\theta \mid x) = \frac{P(x \mid \theta)\,P(\theta)}{P(x)}$$

## A bit of everything

Text with **bold**, *italic*, and `inline code`. A list:

- first item, with $\alpha + \beta = \gamma$
- second item
- third item

A table:

| Symbol | Name | Approx. value |
|---|---|---|
| $\pi$ | pi | 3.14159 |
| $e$ | Euler | 2.71828 |
| $\varphi$ | golden ratio | 1.61803 |

A code block:

```python
import math
print(math.pi)
```

> Closing quote: the math should appear rendered, not as raw text.
