from __future__ import annotations

from dataclasses import dataclass


class BuildPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class BuildPolicy:
    development_build: bool
    formal_release: bool
    tests_required: bool
    preflight_required: bool


def validate_build_policy(
    *,
    git_dirty: bool,
    skip_tests: bool,
    skip_preflight: bool,
    development_build: bool,
) -> BuildPolicy:
    if not development_build and git_dirty:
        raise BuildPolicyError("正式打包要求 Git 工作区干净")
    if not development_build and skip_tests:
        raise BuildPolicyError("正式打包不允许跳过测试")
    if not development_build and skip_preflight:
        raise BuildPolicyError("正式打包不允许跳过 Preflight")
    return BuildPolicy(
        development_build=development_build,
        formal_release=not development_build,
        tests_required=not skip_tests,
        preflight_required=not skip_preflight,
    )
