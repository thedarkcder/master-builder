# Publishing a release

Do not publish until the readiness report's blockers have been cleared by maintainers.
Tests cannot establish IP rights, third-party redistribution permission or clean history.

1. Confirm the organization's license authority and third-party provenance. Retain
   dependency notices and source/license obligations for exactly the distributed bits.
2. Rotate any historical credentials, migrate encrypted data, and privately review
   history, releases, registries, logs and demo assets. Publish a sanitized history or
   clean snapshot only after a reviewed cleanup plan. Recheck remote refs and artifacts.
3. Designate maintainer/security/conduct contacts and supported versions/platforms.
   Configure branch protections and isolated public CI. Keep privileged Jira automation
   disabled unless an operator deliberately configures it for their own instance.
4. Run the complete contributor checks and a fresh-install test on an isolated host.
   Audit locked versions again, including optional voice/GPU/native libraries.
5. Review package contents. Include `LICENSE`, third-party notices, prompts and
   required source/build material; exclude credentials, caches and runtime artifacts.
6. Update versions in Python/UI metadata and locks together, write release notes and
   migration guidance, tag the reviewed commit, and publish checksums/SBOMs.
7. For each distributed image or binary, inventory bundled system packages, SDKs,
   native libraries and models. Source build recipes do not confer redistribution rights.
8. Make corresponding source available as required by the actual AGPL terms. For a
   modified network deployment, review section 13 and provide the required source offer
   to interacting users. Do not add custom commercial or competitive restrictions.

Release publication, registry pushes, deployment and Git-history rewrites require
maintainer execution; this maintenance task performs none of them.
