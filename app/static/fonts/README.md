# Bundled fonts

These fonts are vendored locally so the eximaro appliance can render its
control panel with no internet access. They are loaded by `fonts.css`, which
the app serves from `/static/fonts/`.

Both families are licensed under the **SIL Open Font License, Version 1.1**
(full text in [`OFL.txt`](./OFL.txt)), which permits bundling and redistribution.

These are the two families of the Eximaro "Skylight Mono" design: **Sora** for
headings, the wordmark, and pairing codes; **Hanken Grotesk** for everything you
read. (The earlier Bricolage Grotesque + Space Mono pairing has been retired.)

## Families and weights

| Family (Google Fonts name) | Weight | File |
| --- | --- | --- |
| Sora | 400 | `sora-400.woff2` |
| Sora | 600 | `sora-600.woff2` |
| Sora | 700 | `sora-700.woff2` |
| Hanken Grotesk | 400 | `hanken-grotesk-400.woff2` |
| Hanken Grotesk | 500 | `hanken-grotesk-500.woff2` |
| Hanken Grotesk | 600 | `hanken-grotesk-600.woff2` |
| Hanken Grotesk | 700 | `hanken-grotesk-700.woff2` |

## Source

Downloaded as the `latin` subset, WOFF2 format:

- Sora — <https://fonts.google.com/specimen/Sora>
- Hanken Grotesk — <https://fonts.google.com/specimen/Hanken+Grotesk>

Each weight gets its own filename and `@font-face` rule for clarity and stability.
