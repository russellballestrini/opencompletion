# Vendored browser libraries

Served from our own origin so chat works without third-party CDNs and our
browser tests (tests/functional/) run the exact code people get. Filenames
carry versions; to upgrade, download the new release, rename, update
templates/base.html, and run `make test-ui`.

| File | Source | sha256 |
|---|---|---|
| `highlight-11.6.0.min.js` | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.6.0/highlight.min.js | `e2fda3419c4ae8f6d911e676c65db38a8bfa347324b01160065c6d3195586d09` |
| `marked-9.1.2.min.js` | https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.2/marked.min.js | `b512fc2e8b2d42a00871a5eac88925e4f72bb0696a35050aaaa8c2216225e5b8` |
| `purify-2.5.9.min.js` | https://cdn.jsdelivr.net/npm/dompurify@2.5.9/dist/purify.min.js | `e9508e857549697297e7817e7cfb3a11f2e7489d812ca8cbbd4d0192ffce50ec` |
| `socket.io-4.0.1.min.js` | https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.min.js | `e8da407a321da9d28520d362f6202b458b1f5718240de5d47ab5dbc8911842e7` |
| `highlight-github-11.6.0.min.css` | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.6.0/styles/github.min.css | `3a9a5def8b9c311e5ae43abde85c63133185eed4f0d9f67fea4b00a8308cf066` |
| `highlight-github-dark-11.6.0.min.css` | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.6.0/styles/github-dark.min.css | `9f208d022102b1d0c7aebfecd8e42ca7997d5de636649d2b31ea63093d809019` |
