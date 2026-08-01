# Compliance Boundaries

PAC is intended for personal research and access the user is authorized to
perform. The operator remains responsible for applicable law, site terms,
copyright, privacy, and rate limits.

- Fetch only URLs supplied or explicitly authorized by the user.
- Keep requests bounded: one article by default; batch defaults to 10 and must
  never exceed the hard cap of 25.
- Do not run unbounded discovery or crawling through PAC.
- Do not pass cookies, tokens, passwords, subscription credentials, or other
  secrets.
- Do not bypass CAPTCHA, access-control, SSRF, or bot-challenge safeguards.
- Do not present teaser, partial, or paywall text as a complete article.
- Store or redistribute fetched material only when the user is authorized to do
  so. Quote and attribute sources appropriately.
- Keep monitoring, scheduling, URL discovery, deduplication, and crawl policy in
  the calling scraper; PAC is the bounded fetch component.
