# Grounded code advice

1. Read source before describing behavior. Cite the file and stable symbol/member.
2. Identify evidence strength: `roslyn-index` and `roslyn` are compiler evidence,
   `graph` is a structural match, and `filesystem` is text evidence.
3. Treat `no_results` as absence within `searched_scope`. `unavailable` means an
   analysis layer could not answer; it does not establish absence.
4. Surface caveats, truncation counts, ambiguous matches, dispatch sites and
   candidate sites separately. Mention projects with binding errors.
5. Use compiler symbol keys for follow-ups. Do not silently select an ambiguous symbol.
6. `net48` and `net8` identify configured source roots. Namespaces can occur in both.
7. Cite markup for synthetic or ASP.NET-generated control fields. Template controls
   do not become runtime page-level fields.
8. HTTP route links are route matches, not compiler references. Report link strength.
   Outbound call chains establish static reachability, not execution for a request.
9. DTO twins need their evidence and serializer limitations reported; names alone
   do not establish equivalence.
10. Separate observed behavior from unverified rationale. No history is queried.
11. The query server is read-only. Offer verified diffs for the developer to apply.
12. Call `index_status` when coverage is uncertain. Do not fill gaps with guesses.

For setup and adapting an external codebase, see `docs/ADAPTING.md`. These rules apply
to the codebase queried through the advisor; they do not prohibit maintaining this
source-available advisor implementation itself.
