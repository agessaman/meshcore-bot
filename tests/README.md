# MeshCore Bot Test Suite

This directory contains the test suite for the MeshCore Bot, focusing on graph-based path guessing functionality.

## Structure

```
tests/
├── README.md              # This file
├── conftest.py            # Pytest fixtures and configuration
├── helpers.py             # Test data factories and helper functions
├── unit/                  # Unit tests (isolated, with mocks)
│   ├── test_mesh_graph_edges.py
│   ├── test_mesh_graph_validation.py
│   ├── test_mesh_graph_scoring.py
│   ├── test_mesh_graph_multihop.py
│   └── test_path_command_graph_selection.py
└── integration/          # Integration tests (with real database)
    └── test_path_resolution.py
```

## Running Tests

### Run all tests
```bash
pytest
```

### Run only unit tests
```bash
pytest tests/unit/
```

### Run only integration tests
```bash
pytest tests/integration/
```

### Run specific test file
```bash
pytest tests/unit/test_mesh_graph_edges.py
```

### Run specific test
```bash
pytest tests/unit/test_mesh_graph_edges.py::TestMeshGraphEdges::test_add_new_edge
```

### Run with coverage
```bash
pytest --cov=modules --cov-report=html --cov-report=term-missing
```

### Run with verbose output
```bash
pytest -v
```

### Run with markers
```bash
pytest -m unit          # Run only unit tests
pytest -m integration  # Run only integration tests
pytest -m slow          # Run slow tests
```

## Test Coverage

### Unit Tests

#### `test_mesh_graph_edges.py` (15 tests)
Tests for `MeshGraph` edge management:
- Adding new edges
- Updating existing edges
- Public key handling
- Hop position tracking
- Geographic distance
- Edge queries (get, has, outgoing, incoming)
- Prefix normalization

#### `test_mesh_graph_validation.py` (12 tests)
Tests for path validation:
- Path segment validation
- Confidence calculation
- Recency checks
- Bidirectional edge validation
- Full path validation
- Minimum observations filtering

#### `test_mesh_graph_scoring.py` (11 tests)
Tests for candidate scoring:
- Score calculation with various edge combinations
- Bidirectional bonuses
- Hop position matching
- Geographic distance bonuses
- Minimum observations filtering

#### `test_mesh_graph_multihop.py` (12 tests)
Tests for multi-hop path inference:
- 2-hop and 3-hop path finding
- Intermediate node discovery
- Minimum observations filtering
- Bidirectional path bonuses
- Score reduction for longer paths

#### `test_path_command_graph_selection.py` (8 tests)
Tests for `PathCommand._select_repeater_by_graph`:
- Direct edge selection
- Stored public key bonus
- Star bias multiplier
- Multi-hop inference
- Confidence conversion

### Integration Tests

#### `test_path_resolution.py` (5 tests)
End-to-end tests for full path resolution:
- Path resolution with graph edges from database
- Prefix collision resolution using graph data
- Edge persistence across graph restarts
- Graph vs geographic selection
- Real-world multi-hop scenarios

## Test Fixtures

Fixtures are defined in `conftest.py`:

- `mock_logger`: Mock logger for testing
- `test_config`: Test configuration with Path_Command settings
- `test_db`: In-memory SQLite database for testing
- `mock_bot`: Mock bot instance with all necessary attributes
- `mesh_graph`: Clean `MeshGraph` instance for testing
- `populated_mesh_graph`: `MeshGraph` instance with sample edges

## Test Helpers

Helper functions in `helpers.py`:

- `create_test_repeater()`: Factory for creating test repeater data
- `create_test_edge()`: Factory for creating test edge data
- `create_test_path()`: Factory for creating test path data
- `populate_test_graph()`: Helper to populate a graph with test edges

## Writing New Tests

### Unit Test Example

```python
import pytest
from tests.helpers import create_test_edge

@pytest.mark.unit
class TestMyFeature:
    def test_my_feature(self, mesh_graph):
        """Test description."""
        mesh_graph.add_edge('01', '7e')
        assert mesh_graph.has_edge('01', '7e')
```

### Integration Test Example

```python
import pytest

@pytest.mark.integration
class TestMyIntegration:
    def test_my_integration(self, mock_bot, test_db):
        """Test description."""
        # Use real database and bot components
        pass
```

## Test Markers

Tests are marked with:
- `@pytest.mark.unit`: Unit tests (isolated, with mocks)
- `@pytest.mark.integration`: Integration tests (with real database)
- `@pytest.mark.slow`: Slow-running tests

## Dependencies

Test dependencies are in `requirements.txt`:
- `pytest>=7.0.0`
- `pytest-asyncio>=0.21.0`
- `pytest-mock>=3.10.0`
- `pytest-cov>=4.0.0`

## Configuration

Pytest configuration is in `pytest.ini`:
- Test discovery patterns
- Async test support
- Output options
- Test markers

## Notes

- Unit tests use in-memory SQLite databases for speed
- Integration tests may use real database connections
- All tests should be deterministic and not depend on external services
- Tests should clean up after themselves (fixtures handle this automatically)
