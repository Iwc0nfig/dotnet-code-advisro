# Fictional workspaces

All code here is newly authored demonstration code. `Legacy` and `Modern` share
the `Demo.Shared` namespace to exercise ambiguity and migration-twin queries.

The Website Project illustrates markup controls, code-behind events, a master page,
server comments and JavaScript. The modern project illustrates a minimal API route.
The legacy client contains a matching HTTP URL on the reserved `.invalid` domain;
do not run it to make an HTTP request. Its hand-written JSON is only analysis input,
not a recommended production serialization implementation.

These projects are source-analysis fixtures, not production applications. Compiler
validation copies them to a temporary workspace before building. SDK/targeting-pack
requirements for full compilation are described in `docs/ADAPTING.md`.
