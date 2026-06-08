/**
 * Build-version stamp formatter shared by the topbar (`Layout`) and the
 * Settings footer (`SettingsPanel`).
 *
 * The SPA header shows `v<A>.<B>.<buildNumber> · <shortSha>`. The release
 * semver comes from `web/package.json` (`__APP_VERSION__`), the build number
 * is commits since the latest `vA.B.0` tag (`__APP_BUILD_NUMBER__`), and the
 * commit is the short SHA (`__APP_COMMIT__`).
 *
 * Cloud images bake a *commit-qualified* `APP_VERSION` of the shape
 * `<semver>-commit.<shortSha>` (see `.github/workflows/build-images.yml` and
 * `api/services/upgrade/version_target.py`) so the backend's `__version__`
 * matches a self-upgraded build exactly. That suffix must be stripped before
 * the `A.B.C` split, otherwise the formatter sees four "." segments, bails out,
 * and renders the raw `v0.2.0-commit.<sha> · <sha>` — the commit appears twice
 * and the build version is unreadable.
 */

/**
 * Render the release semver + build number as `A.B.<buildNumber>`.
 *
 * Strips any SemVer pre-release (`-…`) or build-metadata (`+…`) suffix from
 * `releaseVersion` first so a commit-qualified `APP_VERSION`
 * (`0.2.0-commit.2d563cd`) still yields `0.2.<buildNumber>`. Falls back to the
 * (cleaned) release version when the inputs are not the expected shape.
 */
export function formatBuildVersion(releaseVersion: string, buildNumber: string): string {
  // Drop the SemVer pre-release / build-metadata tail (`-commit.<sha>`, `+meta`)
  // so the core `A.B.C` is what we split on.
  const core = releaseVersion.split(/[-+]/, 1)[0] ?? releaseVersion;
  const parts = core.split(".");
  if (parts.length !== 3 || !/^\d+$/.test(buildNumber)) {
    return core;
  }
  return `${parts[0]}.${parts[1]}.${buildNumber}`;
}
