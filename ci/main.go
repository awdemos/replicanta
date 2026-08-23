// CI for Replicanta: test, lint, and the scallopy wheel build.
//
// The test environment needs scallopy 0.2.5 (the Scallop<->Python native
// binding, not on PyPI). Building it takes a pinned Rust nightly and
// ~15 minutes, so CI installs the prebuilt wheel attached to the v0.1.0
// GitHub release — pinned by sha256 so a replaced/rebuilt asset fails the
// install loudly instead of silently executing new native code. Refresh
// that wheel with:
//
//	dagger call build-scallopy --scallop=../scallop export --path=./wheels
//
// then attach the result to a NEW release tag (never clobber an asset in
// place), update wheelURL and wheelSHA256 here together.

package main

import (
	"context"

	"dagger/replicanta/internal/dagger"
)

type Replicanta struct{}

// Prebuilt scallopy wheel (cp314, manylinux_2_39 — glibc floor 2.39, so it
// runs on debian trixie, ubuntu 24.04 and fedora) from the v0.1.0 release.
// The hash pins the exact artifact; pip verifies the #sha256 fragment.
const wheelURL = "https://github.com/awdemos/replicanta/releases/download/v0.1.0/scallopy-0.2.5-cp314-cp314-manylinux_2_39_x86_64.whl"
const wheelSHA256 = "ddc8d190a55681281f50dffe9f12ef1e04b90a38e1196b786ac2ddb9d7ec51be"

// Base images pinned by digest: a mutated or re-tagged upstream image no
// longer changes what CI runs. Bump deliberately.
const pythonSlimImage = "python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4"
const pythonFullImage = "python:3.14@sha256:4fad23465a06cc5149a541fbec6f87e234a64dc0550f6bfdd2d290d8f03240df"
const alpineImage = "alpine:latest@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b"

// rustup-init (x86_64-unknown-linux-gnu) sha256, pinned so the toolchain
// bootstrap never executes an unverified download.
const rustupInitSHA256 = "4acc9acc76d5079515b46346a485974457b5a79893cfb01112423c89aeb5aa10"

// pythonEnv: python 3.14, locked project deps (requirements-ci.txt is
// exported from uv.lock and enforced with --require-hashes), and the
// hash-pinned scallopy wheel.
func (m *Replicanta) pythonEnv(source *dagger.Directory) *dagger.Container {
	return dag.Container().
		From(pythonSlimImage).
		WithMountedCache("/root/.cache/pip", dag.CacheVolume("pip")).
		WithDirectory("/src", source).
		WithWorkdir("/src").
		WithExec([]string{"pip", "install", "--no-input", "--require-hashes",
			"-r", "requirements-ci.txt"}).
		WithExec([]string{"pip", "install", "--no-input", "--no-deps", "-e", "."}).
		WithExec([]string{"pip", "install", "--no-input", wheelURL + "#sha256=" + wheelSHA256})
}

// Run the full test suite.
func (m *Replicanta) Test(ctx context.Context, source *dagger.Directory) (string, error) {
	return m.pythonEnv(source).
		WithExec([]string{"python", "-m", "pytest", "tests", "-q"}).
		Stdout(ctx)
}

// Run ruff. I001 (unsorted imports) and UP017 (datetime.utc) are the
// repo's documented baseline style, so CI enforces "no NEW warnings".
func (m *Replicanta) Lint(ctx context.Context, source *dagger.Directory) (string, error) {
	return m.pythonEnv(source).
		WithExec([]string{"ruff", "check", "--ignore", "I001,UP017", "."}).
		Stdout(ctx)
}

// Lint + test, as CI runs them.
func (m *Replicanta) Ci(ctx context.Context, source *dagger.Directory) (string, error) {
	lint, err := m.Lint(ctx, source)
	if err != nil {
		return "", err
	}
	test, err := m.Test(ctx, source)
	if err != nil {
		return "", err
	}
	return lint + test, nil
}

// Build the scallopy wheel from a scallop source checkout, using the
// pinned Rust nightly the binding requires. Returns the wheels directory
// (filename varies by interpreter/tag). Slow (~15 min) but cached;
// only needed to refresh the wheel attached to a GitHub release.
func (m *Replicanta) BuildScallopy(scallop *dagger.Directory) *dagger.Directory {
	return dag.Container().
		From(pythonFullImage).
		WithMountedCache("/root/.cargo/registry", dag.CacheVolume("cargo-registry")).
		WithMountedCache("/root/.rustup", dag.CacheVolume("rustup")).
		// No curl|sh: download rustup-init, verify it against the pinned
		// sha256, then execute. Toolchain itself stays pinned below.
		WithExec([]string{"bash", "-c",
			"curl --proto '=https' --tlsv1.2 -sSfLO https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init && " +
				"echo '" + rustupInitSHA256 + "  rustup-init' | sha256sum -c - && " +
				"chmod +x rustup-init && ./rustup-init -y --default-toolchain none"}).
		WithExec([]string{"bash", "-c",
			"export PATH=$HOME/.cargo/bin:$PATH && rustup toolchain install nightly-2026-05-24 --profile minimal"}).
		WithExec([]string{"pip", "install", "--no-input", "maturin"}).
		// target/ must be excluded: a warm host target dir lets maturin
		// repackage the host's artifacts (wrong glibc) without rebuilding.
		WithDirectory("/scallop", scallop, dagger.ContainerWithDirectoryOpts{
			Exclude: []string{"target", ".git", ".worktrees"},
		}).
		WithWorkdir("/scallop").
		WithEnvVariable("RUSTUP_TOOLCHAIN", "nightly-2026-05-24").
		WithEnvVariable("PATH", "/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin").
		WithExec([]string{"maturin", "build", "--release",
			"--manifest-path", "etc/scallopy/Cargo.toml"}).
		Directory("target/wheels")
}

// Kept for dagger-call smoke tests: reports the module is alive.
func (m *Replicanta) Ping(ctx context.Context) (string, error) {
	return dag.Container().
		From(alpineImage).
		WithExec([]string{"echo", "replicanta ci module ok"}).
		Stdout(ctx)
}
