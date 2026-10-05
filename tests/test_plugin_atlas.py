"""Focused contract tests for Plugin Atlas v1.

The Atlas is deliberately a small, offline knowledge graph.  These tests
exercise its trust boundaries with synthetic bundles as well as the reviewed
Image-Line catalog.  The fixture bundles use invented names and values; the
only production data read here is the checked-in catalog oracle.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from fl_studio_mcp.plugin_atlas import (
    AtlasLoadError,
    AtlasManifest,
    AtlasRegistry,
    AtlasValidationError,
    AvailabilityObservation,
    CompatibilityJoin,
    LoaderLimits,
    ProductKnowledge,
    RecommendationRequest,
    RuntimeMatch,
    RuntimeParameterObservation,
    RuntimePluginInstance,
    WriteValidationEvidence,
    catalog_name_digest,
    load_atlas,
    load_bundled_registry,
    match_runtime,
    match_runtime_plugin,
    recommend_products,
    recommend_stock_alternatives,
    score_product,
)
from fl_studio_mcp.plugin_atlas.cli import run as run_atlas_cli
from fl_studio_mcp.plugin_atlas.compatibility import join_compatibility
from fl_studio_mcp.plugin_atlas_mcp import (
    AtlasInspectLoadedRequest,
    inspect_loaded_atlas,
)
from fl_studio_mcp.track_b_contracts import (
    MixerEffectTarget,
    TargetedLoadedPluginInventory,
    TargetedPluginSummary,
)


ROOT = Path(__file__).resolve().parents[1]
ATLAS_DATA = ROOT / "fl_studio_mcp" / "plugin_atlas_data"
CURRENT_MATRIX_PATH = ATLAS_DATA / "manifests" / "image-line-current-matrix.json"

CORE_FEATURE_NAMES = (
    "Audio Logger",
    "Chord Generator",
    "Gopher",
    "Loop Starter",
    "Denoising",
    "Sound Content",
    "Audio Clips",
    "Piano Roll",
    "Mixer",
    "Full Song Arrangement",
    "Automation Clips",
    "Time signature changes",
    "MIDI Support",
    "VST2,VST3,AU,CLAP support",
    "FL Studio Remote",
)


def _fixture_products() -> list[dict[str, object]]:
    """Return a tiny valid catalog with explicit stock alternatives."""

    return [
        {
            "id": "p-alpha",
            "vendor_id": "vendor-a",
            "name": "Alpha Compressor",
            "aliases": ["Alpha Comp"],
            "plugin_kinds": ["effect"],
            "kind": "effect",
            "origin": "stock",
            "catalog_scope": "current_edition_matrix",
            "lifecycle": "current",
            "edition_min": "fruity",
            "categories": ["compressor", "dynamics"],
            "problems_solved": ["mud"],
            "appropriate_for": ["vocals", "bass"],
            "techniques": ["t-compression"],
            "stock_alternatives": ["p-stock"],
            "formats": ["native"],
        },
        {
            "id": "p-stock",
            "vendor_id": "vendor-a",
            "name": "Stock Compressor",
            "aliases": ["Stock Comp"],
            "plugin_kinds": ["effect"],
            "kind": "effect",
            "origin": "stock",
            "catalog_scope": "current_edition_matrix",
            "lifecycle": "current",
            "edition_min": "fruity",
            "categories": ["compressor", "dynamics"],
            "problems_solved": ["mud"],
            "appropriate_for": ["vocals"],
            "techniques": ["t-compression"],
            "formats": ["native"],
        },
        {
            "id": "p-synth",
            "vendor_id": "vendor-a",
            "name": "Sine Toy",
            "aliases": ["Sine Instrument"],
            "plugin_kinds": ["instrument"],
            "kind": "instrument",
            "origin": "stock",
            "catalog_scope": "current_edition_matrix",
            "lifecycle": "current",
            "edition_min": "fruity",
            "categories": ["synthesizer"],
            "problems_solved": ["thin bass"],
            "appropriate_for": ["bass", "lead"],
            "techniques": ["t-synthesis"],
            "formats": ["native"],
        },
    ]


def _fixture_resources(products: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "catalog.json": {
            "schema_version": "1.0",
            "vendor": {"id": "vendor-a", "name": "Fixture Vendor", "origin": "stock"},
            "products": _fixture_products() if products is None else products,
        },
        "techniques.json": {
            "techniques": [
                {"id": "t-compression", "name": "Compression"},
                {"id": "t-synthesis", "name": "Synthesis"},
            ]
        },
        "adapters.json": {
            "adapters": [
                {
                    "id": "adapter-alpha",
                    "product_id": "p-alpha",
                    "reported_names": ["Alpha Compressor"],
                    "formats": ["native"],
                    "scope": "mixer_effect",
                    "category": "compressor",
                    "controls": [
                        {
                            "id": "threshold",
                            "parameter_index": 0,
                            "names": ["Threshold"],
                            "kind": "numeric",
                        }
                    ],
                }
            ]
        },
        "evidence.json": {"evidence": []},
    }


def _write_fixture(
    root: Path,
    *,
    products: list[dict[str, object]] | None = None,
    manifest: dict[str, object] | None = None,
    resources: dict[str, object] | None = None,
) -> None:
    payloads = _fixture_resources(products) if resources is None else resources
    manifest_payload: dict[str, object] = {
        "schema_version": "1.0",
        "dataset_id": "fixture-atlas",
        "dataset_version": "fixture-1",
        "resources": [
            {"path": "catalog.json", "kind": "catalog"},
            {"path": "techniques.json", "kind": "techniques"},
            {"path": "adapters.json", "kind": "adapters"},
            {"path": "evidence.json", "kind": "evidence"},
        ],
    }
    if manifest is not None:
        manifest_payload.update(manifest)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(manifest_payload, ensure_ascii=True), encoding="utf-8"
    )
    for relative, payload in payloads.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, bytes):
            destination.write_bytes(payload)
        elif isinstance(payload, str):
            destination.write_text(payload, encoding="utf-8")
        else:
            destination.write_text(
                json.dumps(payload, ensure_ascii=True), encoding="utf-8"
            )


def _load_fixture(
    *, products: list[dict[str, object]] | None = None
) -> AtlasRegistry:
    temporary = tempfile.TemporaryDirectory()
    # The registry owns only immutable Pydantic records; retaining the temp
    # directory on the registry lets tests use this helper without a fixture
    # class lifecycle.  Callers that use this helper do not need the files
    # after loading.
    root = Path(temporary.name)
    _write_fixture(root, products=products)
    registry = AtlasRegistry.load(root)
    _FIXTURE_TEMPS.append(temporary)
    return registry


_FIXTURE_TEMPS: list[tempfile.TemporaryDirectory[str]] = []


class AtlasModelContractTests(unittest.TestCase):
    def test_compatibility_levels_require_their_claimed_proof(self) -> None:
        validated_write = WriteValidationEvidence(
            evidence_id="write-evidence",
            product_id="p",
            adapter_id="adapter-p",
            control_ids=("threshold",),
            status="validated",
            basis="readback_on_a_later_fl_idle_tick",
        )
        with self.assertRaisesRegex(
            ValidationError, "requires adapter and control proof"
        ):
            CompatibilityJoin(
                product_id="p",
                adapter_id="adapter-p",
                availability=AvailabilityObservation(state="loaded"),
                product_match=True,
                adapter_match=True,
                control_proven=False,
                compatibility="write_validated",
                write_validation=(validated_write,),
            )


class AtlasLoaderFixtureTests(unittest.TestCase):
    def test_loads_partitioned_fixture_and_resolves_cross_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Keep the setup visible in this test: no network or package lookup
            # is involved when an explicit root is supplied.
            _write_fixture(Path(directory))
            bundle = load_atlas(Path(directory))
            self.assertEqual([item.product_id for item in bundle.products], [
                "p-alpha", "p-stock", "p-synth"
            ])
            self.assertEqual(bundle.vendors[0].vendor_id, "vendor-a")
            self.assertEqual(bundle.techniques[0].technique_id, "t-compression")
            self.assertEqual(bundle.adapters[0].product_id, "p-alpha")

    def test_duplicate_resource_ids_fail_closed(self) -> None:
        products = _fixture_products()
        products.append(dict(products[0]))
        with tempfile.TemporaryDirectory() as directory:
            _write_fixture(Path(directory), products=products)
            with self.assertRaisesRegex(AtlasValidationError, "duplicate product"):
                load_atlas(Path(directory))

    def test_duplicate_json_object_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_fixture(root)
            (root / "catalog.json").write_text(
                '{"vendor":{"id":"vendor-a","name":"Fixture Vendor"},'
                '"products":[],"products":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AtlasValidationError, "duplicate JSON object key"):
                load_atlas(root)

    def test_duplicate_aliases_are_not_silently_deduplicated(self) -> None:
        products = _fixture_products()
        products[0]["aliases"] = ["same", " same "]
        with tempfile.TemporaryDirectory() as directory:
            _write_fixture(Path(directory), products=products)
            with self.assertRaisesRegex(AtlasValidationError, "alias"):
                load_atlas(Path(directory))

    def test_unknown_vendor_technique_stock_alternative_and_evidence_fail(self) -> None:
        cases = (
            ("vendor_id", "missing-vendor", "unknown vendor"),
            ("techniques", ["missing-technique"], "unknown technique"),
            ("stock_alternatives", ["missing-product"], "unknown stock alternative"),
            ("evidence_ids", ["missing-evidence"], "unknown evidence"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                products = _fixture_products()
                products[0][field] = value
                with tempfile.TemporaryDirectory() as directory:
                    _write_fixture(Path(directory), products=products)
                    with self.assertRaisesRegex(AtlasValidationError, message):
                        load_atlas(Path(directory))

    def test_unknown_adapter_product_and_control_references_fail(self) -> None:
        cases = (
            {"product_id": "missing-product"},
            {
                "product_id": "p-alpha",
                "evidence_ids": ["missing-evidence"],
            },
        )
        for adapter_patch in cases:
            with self.subTest(adapter_patch=adapter_patch):
                resources = _fixture_resources()
                adapter = dict(resources["adapters.json"]["adapters"][0])  # type: ignore[index]
                adapter.update(adapter_patch)
                resources["adapters.json"] = {"adapters": [adapter]}
                with tempfile.TemporaryDirectory() as directory:
                    _write_fixture(Path(directory), resources=resources)
                    with self.assertRaisesRegex(AtlasValidationError, "unknown"):
                        load_atlas(Path(directory))

        resources = _fixture_resources()
        resources["evidence.json"] = {
            "evidence": [
                {
                    "id": "e-control",
                    "product_id": "p-alpha",
                    "adapter_id": "adapter-alpha",
                    "control_ids": ["missing-control"],
                    "level": "read_profiled",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            _write_fixture(Path(directory), resources=resources)
            with self.assertRaisesRegex(AtlasValidationError, "unknown control"):
                load_atlas(Path(directory))

    def test_manifest_count_oracles_and_record_counts_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_fixture(root, manifest={"expected_products": 99})
            with self.assertRaisesRegex(AtlasValidationError, "expected 99 products"):
                load_atlas(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "resources": [
                    {"path": "catalog.json", "kind": "catalog", "record_count": 99}
                ]
            }
            _write_fixture(root, manifest=manifest)
            with self.assertRaisesRegex(AtlasValidationError, "declares 99 records"):
                load_atlas(root)

    def test_resource_paths_are_local_posix_paths_only(self) -> None:
        for unsafe in ("../catalog.json", "/tmp/catalog.json", "catalog\\x.json"):
            with self.subTest(path=unsafe), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_fixture(root, manifest={
                    "resources": [{"path": unsafe, "kind": "catalog"}]
                })
                with self.assertRaisesRegex(AtlasValidationError, "path"):
                    load_atlas(root)

    def test_invalid_json_and_invalid_utf8_are_rejected(self) -> None:
        for invalid in ("{", b"\xff\xfe"):
            with self.subTest(invalid=repr(invalid)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_fixture(root)
                if isinstance(invalid, bytes):
                    (root / "catalog.json").write_bytes(invalid)
                else:
                    (root / "catalog.json").write_text(invalid, encoding="utf-8")
                with self.assertRaisesRegex(AtlasValidationError, "invalid JSON"):
                    load_atlas(root)

    def test_loader_is_atomic_after_a_late_resource_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_fixture(root)
            (root / "adapters.json").write_text("not-json", encoding="utf-8")
            loader = __import__(
                "fl_studio_mcp.plugin_atlas.loader", fromlist=["AtlasLoader"]
            ).AtlasLoader(root)
            with self.assertRaises(AtlasValidationError):
                loader.load()
            (root / "adapters.json").write_text(
                json.dumps(_fixture_resources()["adapters.json"]), encoding="utf-8"
            )
            bundle = loader.load()
            self.assertEqual(len(bundle.products), 3)
            self.assertEqual(len(bundle.adapters), 1)

    def test_limits_cover_loader_configuration_bytes_depth_nodes_and_counts(self) -> None:
        bad_limits = (
            {"max_resource_bytes": 0},
            {"max_total_resource_bytes": 0},
            {"max_resources": 0},
            {"max_json_depth": 0},
            {"max_json_nodes": 0},
        )
        for values in bad_limits:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    LoaderLimits(**values)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_fixture(root)
            with self.assertRaisesRegex(AtlasLoadError, "byte limit"):
                load_atlas(root, limits=LoaderLimits(max_resource_bytes=128))

        deeply_nested: object = "leaf"
        for _ in range(40):
            deeply_nested = [deeply_nested]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_fixture(root, resources={
                "catalog.json": deeply_nested,
                "techniques.json": {"techniques": []},
                "adapters.json": {"adapters": []},
                "evidence.json": {"evidence": []},
            })
            with self.assertRaisesRegex(AtlasLoadError, "depth"):
                load_atlas(root, limits=LoaderLimits(max_json_depth=8))

    def test_catalog_name_digest_is_order_independent_and_text_canonical(self) -> None:
        self.assertEqual(
            catalog_name_digest([" Beta ", "Alpha"]),
            catalog_name_digest(["Alpha", "Beta"]),
        )
        self.assertNotEqual(catalog_name_digest(["Alpha"]), catalog_name_digest(["alpha"]))

    def test_fixture_registry_digest_is_stable_across_product_order(self) -> None:
        first = _load_fixture()
        reversed_products = list(reversed(_fixture_products()))
        second = _load_fixture(products=reversed_products)
        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(
            [item.product_id for item in first.search("compressor")],
            [item.product_id for item in second.search("compressor")],
        )


class BundledCatalogOracleTests(unittest.TestCase):
    # Row counts, digests and category counts are declared in the Atlas
    # manifest and enforced by the loader on every load; these tests cover the
    # cross-file relationships the loader does not.
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_bundled_registry()
        cls.oracle = json.loads(CURRENT_MATRIX_PATH.read_text(encoding="utf-8"))
        snapshots = [
            snapshot
            for snapshot in cls.registry.catalog_snapshots
            if snapshot.catalog_scope == "current_edition_matrix"
        ]
        assert len(snapshots) == 1, snapshots
        cls.snapshot = snapshots[0]

    def test_current_matrix_snapshot_matches_the_checked_in_oracle(self) -> None:
        actual = [
            {"category": row.category, "edition_min": row.edition_min, "name": row.name}
            for row in self.snapshot.rows
        ]
        self.assertEqual(actual, self.oracle)

    def test_snapshot_resolves_every_current_product(self) -> None:
        products_by_id = {product.product_id: product for product in self.registry.products}
        product_ids = [row.product_id for row in self.snapshot.rows]
        self.assertTrue(all(product_ids))
        self.assertEqual(len(set(product_ids)), len(product_ids))
        for row in self.snapshot.rows:
            with self.subTest(product=row.product_id):
                self.assertIn(row.product_id, products_by_id)
                product = products_by_id[row.product_id]
                self.assertEqual(product.name, row.name)
                self.assertEqual(product.edition_min, row.edition_min)
                self.assertEqual(product.catalog_scope, "current_edition_matrix")

    def test_legacy_and_core_feature_rows_stay_out_of_the_current_matrix(self) -> None:
        image_line = [item for item in self.registry.products if item.vendor_id == "image-line"]
        matrix_names = {row["name"] for row in self.oracle}
        current_names = {
            item.name
            for item in image_line
            if item.catalog_scope == "current_edition_matrix"
        }
        self.assertEqual(current_names, matrix_names)
        legacy_names = {
            item.name
            for item in image_line
            if item.catalog_scope == "legacy_discontinued"
        }
        self.assertTrue(legacy_names)
        self.assertTrue(
            matrix_names.isdisjoint(legacy_names),
            "legacy/deprecated rows must not pollute the current pricing matrix",
        )
        self.assertTrue(
            set(CORE_FEATURE_NAMES).isdisjoint({item.name for item in image_line}),
            "core DAW features are not PluginKnowledge records",
        )


class RegistryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = _load_fixture()

    def test_lookup_unknowns_and_exact_id_name_alias(self) -> None:
        self.assertEqual(self.registry.require_product("p-alpha").name, "Alpha Compressor")
        self.assertEqual(self.registry.find_product("alpha comp").product_id, "p-alpha")
        self.assertEqual(self.registry.find_product("ALPHA COMPRESSOR").product_id, "p-alpha")
        self.assertIsNone(self.registry.product("missing"))
        self.assertIsNone(self.registry.vendor("missing"))
        self.assertIsNone(self.registry.technique("missing"))
        self.assertIsNone(self.registry.adapter("missing"))
        self.assertEqual(self.registry.adapters_for_product("missing"), ())
        self.assertEqual(self.registry.stock_alternatives("missing"), ())
        with self.assertRaisesRegex(KeyError, "unknown Atlas product"):
            self.registry.require_product("missing")

    def test_ambiguous_alias_does_not_choose_an_arbitrary_product_but_ids_are_exact(self) -> None:
        shared_a = ProductKnowledge(product_id="p-a", name="A", aliases=["Shared FX"])
        shared_b = ProductKnowledge(product_id="p-b", name="B", aliases=["shared fx"])
        ambiguous = AtlasRegistry.from_parts(
            AtlasManifest(dataset_version="test"), products=(shared_b, shared_a)
        )
        self.assertIsNone(ambiguous.find_product("shared fx"))

        by_id = AtlasRegistry.from_parts(
            AtlasManifest(dataset_version="test"),
            products=(
                ProductKnowledge(product_id="other", name="exact-id"),
                ProductKnowledge(product_id="exact-id", name="Primary"),
            ),
        )
        self.assertEqual(by_id.find_product("exact-id").product_id, "exact-id")

    def test_search_filters_and_bounds_are_deterministic(self) -> None:
        self.assertEqual(
            [item.product_id for item in self.registry.search("compressor")],
            ["p-alpha", "p-stock"],
        )
        self.assertEqual(
            [item.product_id for item in self.registry.search("", kind="instrument")],
            ["p-synth"],
        )
        self.assertEqual(
            [item.product_id for item in self.registry.search("", stock_only=True)],
            ["p-alpha", "p-stock", "p-synth"],
        )
        self.assertEqual(self.registry.search("no such product"), ())
        for query in (None, 1, True):
            with self.subTest(query=query), self.assertRaises(TypeError):
                self.registry.search(query)  # type: ignore[arg-type]
        for limit in (0, 129, False):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.registry.search_hits("", limit=limit)  # type: ignore[arg-type]


class MatcherAndAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = _load_fixture()

    def test_exact_name_match_without_parameters_is_name_only_not_control_proof(self) -> None:
        match = match_runtime_plugin(
            RuntimePluginInstance(name="Alpha Compressor", format="native"),
            self.registry,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.product_id, "p-alpha")
        self.assertEqual(match.control_status, "name_only")
        self.assertFalse(match.control_proven)
        self.assertFalse(match.parameter_evidence)
        self.assertIn("product name alone is not control proof", match.reasons)

    def test_matching_parameter_observation_adds_explicit_control_evidence(self) -> None:
        runtime = RuntimePluginInstance(
            name="Alpha Compressor",
            format="native",
            parameters=(
                RuntimeParameterObservation(
                    index=0, name="Threshold", display="-10 dB", kind="numeric"
                ),
            ),
        )
        match = match_runtime_plugin(runtime, self.registry)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.control_status, "evidence")
        self.assertTrue(match.control_proven)
        self.assertEqual(match.parameter_evidence[0].control_id, "threshold")

    def test_runtime_mapping_is_strict_but_accepts_bridge_aliases(self) -> None:
        matches = match_runtime(
            {
                "plugin_name": "Alpha Compressor",
                "plugin_format": "native",
                "params": [{"index": 0, "reported_name": "Threshold", "display_text": "-10 dB"}],
            },
            self.registry,
        )
        self.assertTrue(matches)
        self.assertEqual(matches[0].product_id, "p-alpha")
        with self.assertRaises(ValueError):
            match_runtime({"name": "Alpha Compressor", "parameters": ["bad"]}, self.registry)

    def test_exact_vs_ambiguous_aliases_never_promote_name_to_control_proof(self) -> None:
        products = _fixture_products()
        products[0]["aliases"] = ["Shared FX"]
        products[1]["aliases"] = ["shared fx"]
        ambiguous = AtlasRegistry.from_parts(
            AtlasManifest(dataset_version="test"),
            products=tuple(
                ProductKnowledge.model_validate(item) for item in products
            ),
        )
        matches = match_runtime(
            RuntimePluginInstance(name="shared fx"), ambiguous, include_weak=True
        )
        self.assertGreaterEqual(len(matches), 2)
        self.assertTrue(all(not match.control_proven for match in matches))

    def test_compatibility_join_keeps_name_only_and_warns_on_unloaded_state(self) -> None:
        runtime = RuntimePluginInstance(
            name="Alpha Compressor",
            availability=AvailabilityObservation(state="not_observed"),
        )
        joined = join_compatibility(runtime, self.registry)
        self.assertEqual(joined.compatibility, "name_only")
        self.assertFalse(joined.control_proven)
        self.assertFalse(joined.adapter_match)
        self.assertIn("ownership and installation remain unknown", " ".join(joined.limitations))

    def test_mismatched_write_evidence_cannot_promote_a_runtime_match(self) -> None:
        runtime = RuntimePluginInstance(
            name="Alpha Compressor",
            format="native",
            parameters=(RuntimeParameterObservation(index=0, name="Threshold"),),
        )
        match = match_runtime_plugin(runtime, self.registry)
        self.assertIsNotNone(match)
        assert match is not None
        mismatched = WriteValidationEvidence(
            evidence_id="wrong-product-write",
            product_id="p-stock",
            adapter_id="adapter-alpha",
            control_ids=("threshold",),
            status="validated",
            basis="readback_on_a_later_fl_idle_tick",
        )
        with self.assertRaisesRegex(ValueError, "not 'p-alpha'"):
            join_compatibility(
                match,
                self.registry,
                write_validation=(mismatched,),
            )


class RecommendationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = _load_fixture()

    @staticmethod
    def _loaded_match(state: str = "loaded") -> RuntimeMatch:
        return RuntimeMatch(
            instance_id="mixer_effect:2:0",
            product_id="p-alpha",
            adapter_id="adapter-alpha",
            availability=AvailabilityObservation(state=state),  # type: ignore[arg-type]
            name_score=1.0,
            parameter_score=0.0,
            overall_score=0.6,
            confidence="medium",
            control_status="name_only",
        )

    def test_recommendations_are_deterministic_and_report_unknown_availability(self) -> None:
        request = RecommendationRequest(query="compressor", limit=3)
        first = recommend_products(self.registry, request)
        second = recommend_products(self.registry, request)
        self.assertEqual(first, second)
        self.assertEqual([row.product_id for row in first], ["p-alpha", "p-stock"])
        self.assertTrue(all(row.availability.state == "availability_unknown" for row in first))

        with_match = recommend_products(
            self.registry, request, loaded_matches=(self._loaded_match("not_observed"),)
        )
        alpha = next(row for row in with_match if row.product_id == "p-alpha")
        self.assertEqual(alpha.availability.state, "not_observed")
        self.assertEqual(alpha.loaded_instance_id, "mixer_effect:2:0")

    def test_stock_alternatives_include_explicit_and_inferred_stock_rows(self) -> None:
        alternatives = recommend_stock_alternatives(self.registry, "p-alpha")
        self.assertEqual([row.product_id for row in alternatives], ["p-stock"])
        self.assertTrue(all(row.stock_alternative for row in alternatives))
        self.assertTrue(all(row.source_product_id == "p-alpha" for row in alternatives))
        self.assertIn("explicit stock alternative", alternatives[0].reasons)
        with self.assertRaisesRegex(KeyError, "unknown Atlas product"):
            recommend_stock_alternatives(self.registry, "missing")

    def test_kind_filters_even_exact_names_and_stock_tiebreakers(self) -> None:
        self.assertEqual(recommend_products(self.registry, RecommendationRequest(
            query="Alpha Compressor", kind="instrument", prefer_stock=True,
        )), ())
        self.assertEqual([row.product_id for row in recommend_products(
            self.registry, RecommendationRequest(kind="instrument", prefer_stock=True),
        )], ["p-synth"])
        product = self.registry.product("p-alpha")
        assert product is not None
        self.assertEqual(score_product(product, RecommendationRequest(kind="instrument")), 0.0)

    def test_poor_fit_and_stock_or_loaded_status_cannot_create_relevance(self) -> None:
        product = self.registry.product("p-alpha")
        assert product is not None
        negative = product.model_copy(update={"poor_fit_when": ("granular resynthesis",)})
        registry = AtlasRegistry.from_bundle(self.registry.bundle.model_copy(update={"products": (negative,)}))
        self.assertEqual(recommend_products(
            registry, RecommendationRequest(query="granular resynthesis", prefer_stock=True),
            loaded_matches=(self._loaded_match(),),
        ), ())
        self.assertEqual(score_product(negative, "granular resynthesis"), 0.0)

    def test_bundled_bass_instrument_search_excludes_bass_effects(self) -> None:
        registry = load_bundled_registry()
        rows = recommend_products(registry, RecommendationRequest(query="bass", kind="instrument", limit=128))
        self.assertGreater(len(rows), 3)
        self.assertNotIn("image-line.fruity-bass-boost", {row.product_id for row in rows})
        self.assertIn("image-line.3x-osc", {row.product_id for row in rows})
        for row in rows:
            product = registry.product(row.product_id)
            assert product is not None
            self.assertTrue(product.kind == "instrument" or "instrument" in product.plugin_kinds)

    def test_common_instruments_are_usable_source_knowledge(self) -> None:
        registry = load_bundled_registry()
        rows = recommend_products(registry, RecommendationRequest(
            sources=("piano",), kind="instrument", limit=128,
        ))
        self.assertIn("image-line.fl-keys", {row.product_id for row in rows})

    def test_natural_request_filler_does_not_make_every_instrument_relevant(self) -> None:
        registry = load_bundled_registry()
        rows = recommend_products(registry, RecommendationRequest(
            query="I want a plugin for bass", kind="instrument", limit=128,
        ))
        ids = {row.product_id for row in rows}
        self.assertIn("image-line.boobass", ids)
        self.assertNotIn("image-line.fl-keys", ids)
        self.assertNotIn("image-line.fpc", ids)

    def test_recommendation_limits_are_bounded(self) -> None:
        for limit in (0, 129):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                recommend_products(self.registry, limit=limit)


class AtlasCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = _load_fixture()

    def test_cli_is_offline_and_supports_digest_search_and_show(self) -> None:
        output = io.StringIO()
        with mock.patch(
            "fl_studio_mcp.plugin_atlas.cli.load_bundled_registry",
            side_effect=AssertionError("injected registry should avoid package loading"),
        ):
            self.assertEqual(
                run_atlas_cli(["digest", "--json"], registry=self.registry, stdout=output),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["digest"], self.registry.digest())

        search_output = io.StringIO()
        self.assertEqual(
            run_atlas_cli(
                ["search", "compressor", "--json"],
                registry=self.registry,
                stdout=search_output,
            ),
            0,
        )
        self.assertEqual(
            [row["product"]["product_id"] for row in json.loads(search_output.getvalue())],
            ["p-alpha", "p-stock"],
        )

        show_output = io.StringIO()
        self.assertEqual(
            run_atlas_cli(["show", "p-alpha"], registry=self.registry, stdout=show_output),
            0,
        )
        self.assertEqual(json.loads(show_output.getvalue())["name"], "Alpha Compressor")

    def test_cli_unknown_product_is_a_bounded_error(self) -> None:
        errors = io.StringIO()
        self.assertEqual(
            run_atlas_cli(["show", "missing"], registry=self.registry, stderr=errors),
            2,
        )
        self.assertIn("postfader-plugin-atlas", errors.getvalue())
        self.assertIn("missing", errors.getvalue())


class GenericDiscoveryAndMixCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = _load_fixture()

    def test_generic_loaded_discovery_keeps_unlisted_identity_independent_plugins(self) -> None:
        target = MixerEffectTarget(track_index=2, slot_index=0)
        inventory = TargetedLoadedPluginInventory(
            observed_at=datetime.now(timezone.utc),
            plugins=[
                TargetedPluginSummary(
                    target=target,
                    name="Vendor Mystery FX",
                    reported_parameter_count=7,
                    mix_level_normalized=1.0,
                )
            ],
        )
        response = inspect_loaded_atlas(
            AtlasInspectLoadedRequest(),
            registry=self.registry,
            inventory=inventory,
        )
        self.assertEqual(len(response.plugins), 1)
        record = response.plugins[0]
        self.assertEqual(record.target, target)
        self.assertEqual(record.plugin.name, "Vendor Mystery FX")
        self.assertEqual(record.runtime.name, "Vendor Mystery FX")
        self.assertEqual(record.matches, ())
        self.assertIsNone(record.best_match)
        self.assertIsNone(record.compatibility)


if __name__ == "__main__":
    unittest.main()
