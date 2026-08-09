// CI for Replicanta: test, lint, and the scallopy wheel build.
//
// The test environment needs scallopy 0.2.5 (the Scallop<->Python native
// binding, not on PyPI). Building it takes a pinned Rust nightly and
// ~15 minutes, so CI installs the prebuilt wheel attached to the v0.1.0
// GitHub release. Refresh that wheel with:
//
//	dagger call build-scallopy --scallop=../scallop export --path=./wheels
//
// and attach the result to a new release (then update wheelURL here).

package main

import (
	"context"

	"dagger/replicanta/internal/dagger"
)

type Replicanta struct{}

// Prebuilt scallopy wheel (cp314, manylinux_2_39 — glibc floor 2.39, so it
// runs on debian trixie, ubuntu 24.04 and fedora) from the v0.1.0 release.
const wheelURL = "https://github.com/awdemos/replicanta/releases/download/v0.1.0/scallopy-0.2.5-cp314-cp314-manylinux_2_39_x86_64.whl"

// Bump whenever the release wheel is replaced — the layer cache keys on
// the command string, not the URL content, and stale wheels are fatal.
// 1 = fedora build (broken: glibc 2.43), 2 = accidental repackage of the
// fedora artifacts (target/ leaked into the build context), 3 = clean
// debian bookworm build.
const wheelBuild = "3"

// pythonEnv: python 3.14, project deps, pytest/ruff, and the scallopy wheel.
func (m *Replicanta) pythonEnv(source *dagger.Directory) *dagger.Container {
	return dag.Container().
		From("python:3.14-slim").
		WithMountedCache("/root/.cache/pip", dag.CacheVolume("pip")).
		WithDirectory("/src", source).
		WithWorkdir("/src").
		WithExec([]string{"pip", "install", "--no-input", "-e", ".", "pytest", "ruff"}).
		WithEnvVariable("WHEEL_BUILD", wheelBuild).
		WithExec([]string{"pip", "install", "--no-input", wheelURL})
}

// Run the full test suite (418 tests).
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
// only needed to refresh the wheel attached to the GitHub release.
func (m *Replicanta) BuildScallopy(scallop *dagger.Directory) *dagger.Directory {
	return dag.Container().
		From("python:3.14").
		WithMountedCache("/root/.cargo/registry", dag.CacheVolume("cargo-registry")).
		WithMountedCache("/root/.rustup", dag.CacheVolume("rustup")).
		WithExec([]string{"bash", "-c",
			"curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain none"}).
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
		From("alpine:latest").
		WithExec([]string{"echo", "replicanta ci module ok"}).
		Stdout(ctx)
}
