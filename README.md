# tensionix.github.io

Source of <https://tensionix.github.io>: the Audion projects published on this GitHub account, arranged for people — what each tool does, its latest version and a direct download.

The site maintains itself. Every page is built from the GitHub API — the profile, repository descriptions, topics and latest releases — so nothing here is written by hand except layout and style.

The GraphQL fetch starts with 10 repositories per page. An HTTP 502 immediately
reduces the page to 5, then 1, retrying the same cursor without losing successful
pages. The reduced size is retained for the rest of the run. At size 1, the
normal four-attempt retry policy applies. Other errors keep their existing
retry policy; GraphQL errors, invalid pagination and duplicate repositories are
fatal. No generated site or saved API snapshot is written from an incomplete
repository collection, and a failed build cannot reach the deploy job.

Offline regression tests run before the API fetch in GitHub Actions:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

`icons/<repository>.svg` or `.png` — a project icon that takes precedence over the one found in the project's own repository.
