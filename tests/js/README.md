# JavaScript tests

Run with a Node.js + jsdom environment:

    npm install jsdom
    node tests/js/member_search.test.js

## member_search.test.js

Guards `static/js/member-search.js`, the shared member typeahead used by the
benevolent register/contribution/case forms, the membership detail page, and
the pledge form.

It exists because that widget shipped with a bug that made it **never display
a single suggestion to anybody**: `query()` resolved to the endpoint's JSON
envelope `{results: [...]}`, and that whole object was handed to
`renderResults()`, which immediately tested `results.length` — `undefined` on
an object — and hid the box and returned. The endpoint was fine. The CSS was
fine. The request was even being made. The answer was thrown away one line
before it could be rendered, on every keystroke, in every form that used it.

Nothing in the Django test suite could see that, because the failure lived
entirely in the browser. Hence this file.
