import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from loguru import logger

import codec.schema_pb2 as pb

NodePropertyValue = str | int | bool | list[str] | None
NodeProperties = dict[str, NodePropertyValue]


class ProtobufFileIngestor:
    """
    Handles parsing graph nodes & relationships directly into a compact Protobuf
    serialsed file for efficient extraction & transmission of data without needing
    to build the graph first
    """

    LABEL_TO_ONEOF_FIELD: dict[str, str] = {
        "Project": "project",
        "Package": "package",
        "Folder": "folder",
        "Module": "module",
        "Class": "class_node",
        "Function": "function",
        "Method": "method",
        "File": "file",
        "ExternalPackage": "external_package",
        "ModuleImplementation": "module_implementation",
        "ModuleInterface": "module_interface",
    }

    ONEOF_FIELD_TO_LABEL: dict[str, str] = {
        v: k for k, v in LABEL_TO_ONEOF_FIELD.items()
    }

    def __init__(self, output_path: str, split_index: bool = False):
        self.output_dir = Path(output_path)
        self._nodes: dict[str, pb.Node] = {}
        self._relationships: dict[tuple[str, int, str], pb.Relationship] = {}
        self.split_index = split_index
        logger.info(f"ProtobufFileIngestor initialized to write to: {self.output_dir}")

    def _get_node_id(self, label: str, properties: NodeProperties) -> str:
        """Determines the primary/node key for a node."""
        if label in ["Folder", "File"]:
            return str(properties.get("path", ""))
        elif label in ["ExternalPackage", "Project"]:
            return str(properties.get("name", ""))
        else:
            return str(properties.get("qualified_name", ""))

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
        """Creates a protobuf Node message and adds it to the in-memory buffer."""
        node_id = self._get_node_id(label, properties)
        if not node_id or node_id in self._nodes:
            return

        payload_message_class = getattr(pb, label, None)
        if not payload_message_class:
            logger.warning(
                f"No Protobuf message class found for label '{label}'. Skipping node."
            )
            return

        payload_message = payload_message_class()

        for key, value in properties.items():
            if hasattr(payload_message, key):
                if value is None:
                    continue
                destination_attribute = getattr(payload_message, key)
                if hasattr(destination_attribute, "extend") and isinstance(value, list):
                    destination_attribute.extend(value)
                else:
                    setattr(payload_message, key, value)

        node = pb.Node()

        payload_field_name = self.LABEL_TO_ONEOF_FIELD.get(label)
        if not payload_field_name:
            logger.warning(
                f"No 'oneof' field mapping found for label '{label}'. Skipping node."
            )
            return

        getattr(node, payload_field_name).CopyFrom(payload_message)

        self._nodes[node_id] = node

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, Any],
        rel_type: str,
        to_spec: tuple[str, str, Any],
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Creates a protobuf Relationship message and adds it to the buffer."""
        rel = pb.Relationship()

        try:
            rel.type = pb.Relationship.RelationshipType.Value(rel_type)  # type: ignore[misc,assignment]
        except ValueError:
            logger.warning(
                f"Unknown relationship type '{rel_type}'. Setting to UNSPECIFIED."
            )
            rel.type = pb.Relationship.RelationshipType.RELATIONSHIP_TYPE_UNSPECIFIED  # type: ignore[misc]

        from_label, _, from_val = from_spec
        to_label, _, to_val = to_spec

        rel.source_id = str(from_val)  # type: ignore[misc]
        rel.source_label = str(from_label)  # type: ignore[misc]
        rel.target_id = str(to_val)  # type: ignore[misc]
        rel.target_label = str(to_label)  # type: ignore[misc]

        if rel.source_id.strip() == "" or rel.target_id.strip() == "":
            logger.warning(
                f"Invalid relationship: source_id={rel.source_id}, target_id={rel.target_id}"
            )
            return

        if properties:
            rel.properties.update(properties)

        unique_key = (rel.source_id, rel.type, rel.target_id)
        if unique_key in self._relationships:
            existing_rel = self._relationships[unique_key]
            if properties:
                existing_rel.properties.update(properties)
        else:
            self._relationships[unique_key] = rel

    def _flush_joint(self) -> None:
        """Assembles index into a single Protobuf file"""

        index = pb.GraphCodeIndex()
        index.nodes.extend(self._nodes.values())
        index.relationships.extend(self._relationships.values())

        serialised_file = index.SerializeToString()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / "index.bin"
        with open(out_path, "wb") as f:
            f.write(serialised_file)

        logger.success(
            f"Successfully flushed {len(self._nodes)} unique nodes and {len(self._relationships)} unique relationships to {self.output_dir}"
        )

    def _flush_split(self) -> None:
        """Assembles index into two separate binary files in the output directory:
        'nodes.bin' and 'relationships.bin'."""

        nodes_index = pb.GraphCodeIndex()
        rels_index = pb.GraphCodeIndex()
        nodes_index.nodes.extend(self._nodes.values())
        rels_index.relationships.extend(self._relationships.values())

        serialised_nodes = nodes_index.SerializeToString()
        serialised_rels = rels_index.SerializeToString()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        nodes_path = self.output_dir / "nodes.bin"
        rels_path = self.output_dir / "relationships.bin"

        with open(nodes_path, "wb") as f:
            f.write(serialised_nodes)

        with open(rels_path, "wb") as f:
            f.write(serialised_rels)

        logger.success(
            f"Successfully flushed {len(self._nodes)} unique nodes and {len(self._relationships)} unique relationships to {self.output_dir}"
        )

    def flush_all(self) -> None:
        """Assembles and writes the final binary file(s)"""
        logger.info(f"Flushing data to {self.output_dir}...")

        if self.split_index:
            return self._flush_split()
        else:
            return self._flush_joint()


class ProtobufIndexReader:
    """
    Reads and deserializes protobuf index files created by ProtobufFileIngestor.
    Supports both joint (index.bin) and split (nodes.bin + relationships.bin) modes.
    """

    # Reverse mapping from oneof field to label
    ONEOF_FIELD_TO_LABEL: dict[str, str] = {
        v: k for k, v in ProtobufFileIngestor.LABEL_TO_ONEOF_FIELD.items()
    }

    # Map label to primary key property name
    LABEL_TO_ID_PROPERTY: dict[str, str] = {
        "Project": "name",
        "Package": "qualified_name",
        "Folder": "path",
        "Module": "qualified_name",
        "Class": "qualified_name",
        "Function": "qualified_name",
        "Method": "qualified_name",
        "File": "path",
        "ExternalPackage": "name",
        "ModuleImplementation": "qualified_name",
        "ModuleInterface": "qualified_name",
    }

    def __init__(self, index_path: str | Path):
        """
        Initialize the reader with a path to the index.

        Args:
            index_path: Path to index.bin file or directory containing
                        nodes.bin and relationships.bin
        """
        self.index_path = Path(index_path)
        self._nodes: list[pb.Node] = []
        self._relationships: list[pb.Relationship] = []
        self._mode: str | None = None  # 'joint' or 'split'

    def detect_mode(self) -> str:
        """
        Detect whether index is in joint or split mode.

        Returns:
            'joint' if index.bin exists, 'split' if nodes.bin/relationships.bin exist

        Raises:
            FileNotFoundError: If no valid index files found
        """
        if self.index_path.is_file():
            # Direct path to index.bin
            self._mode = "joint"
            return "joint"

        if self.index_path.is_dir():
            joint_file = self.index_path / "index.bin"
            nodes_file = self.index_path / "nodes.bin"
            rels_file = self.index_path / "relationships.bin"

            if joint_file.exists():
                self._mode = "joint"
                return "joint"
            elif nodes_file.exists() and rels_file.exists():
                self._mode = "split"
                return "split"

        raise FileNotFoundError(
            f"No valid index found at {self.index_path}. "
            "Expected index.bin or nodes.bin + relationships.bin"
        )

    def load(self) -> tuple[list[pb.Node], list[pb.Relationship]]:
        """
        Load and deserialize the protobuf index.

        Returns:
            Tuple of (nodes, relationships) lists
        """
        if self._mode is None:
            self.detect_mode()

        if self._mode == "joint":
            return self._load_joint()
        else:
            return self._load_split()

    def _load_joint(self) -> tuple[list[pb.Node], list[pb.Relationship]]:
        """Load from a single index.bin file."""
        file_path = (
            self.index_path
            if self.index_path.is_file()
            else self.index_path / "index.bin"
        )

        logger.info(f"Loading joint index from: {file_path}")

        with open(file_path, "rb") as f:
            data = f.read()

        index = pb.GraphCodeIndex()
        index.ParseFromString(data)

        self._nodes = list(index.nodes)
        self._relationships = list(index.relationships)

        logger.info(
            f"Loaded {len(self._nodes)} nodes and {len(self._relationships)} relationships"
        )
        return self._nodes, self._relationships

    def _load_split(self) -> tuple[list[pb.Node], list[pb.Relationship]]:
        """Load from separate nodes.bin and relationships.bin files."""
        nodes_path = self.index_path / "nodes.bin"
        rels_path = self.index_path / "relationships.bin"

        logger.info(f"Loading split index from: {self.index_path}")

        # Load nodes
        with open(nodes_path, "rb") as f:
            nodes_data = f.read()
        nodes_index = pb.GraphCodeIndex()
        nodes_index.ParseFromString(nodes_data)
        self._nodes = list(nodes_index.nodes)

        # Load relationships
        with open(rels_path, "rb") as f:
            rels_data = f.read()
        rels_index = pb.GraphCodeIndex()
        rels_index.ParseFromString(rels_data)
        self._relationships = list(rels_index.relationships)

        logger.info(
            f"Loaded {len(self._nodes)} nodes and {len(self._relationships)} relationships"
        )
        return self._nodes, self._relationships

    def extract_node_data(self, node: pb.Node) -> tuple[str, NodeProperties] | None:
        """
        Extract label and properties from a protobuf Node.

        Args:
            node: A protobuf Node message

        Returns:
            Tuple of (label, properties_dict) or None if invalid
        """
        # Determine which oneof field is set
        field_name = node.WhichOneof("payload")
        if not field_name:
            logger.warning("Node has no payload set")
            return None

        label = self.ONEOF_FIELD_TO_LABEL.get(field_name)
        if not label:
            logger.warning(f"Unknown oneof field: {field_name}")
            return None

        # Get the payload message
        payload = getattr(node, field_name)

        # Extract all properties from the payload
        properties: NodeProperties = {}
        for field in payload.DESCRIPTOR.fields:
            value = getattr(payload, field.name)
            # Handle repeated fields (lists)
            if field.label == field.LABEL_REPEATED:
                properties[field.name] = list(value) if value else []
            elif value or field.type == field.TYPE_BOOL:
                # Include non-empty values and booleans
                properties[field.name] = value

        return label, properties

    def extract_relationship_data(
        self, rel: pb.Relationship
    ) -> tuple[tuple[str, str, str], str, tuple[str, str, str], dict[str, Any]] | None:
        """
        Extract relationship data in MemgraphIngestor format.

        Args:
            rel: A protobuf Relationship message

        Returns:
            Tuple of (from_spec, rel_type, to_spec, properties) or None if invalid
            - from_spec: (label, key_property, value)
            - to_spec: (label, key_property, value)
        """
        # Get relationship type name from enum
        rel_type_name = pb.Relationship.RelationshipType.Name(rel.type)
        if rel_type_name == "RELATIONSHIP_TYPE_UNSPECIFIED":
            logger.warning("Skipping relationship with unspecified type")
            return None

        source_label = rel.source_label
        target_label = rel.target_label

        if not source_label or not target_label:
            logger.warning(
                f"Relationship missing label: source={source_label}, target={target_label}"
            )
            return None

        # Get the key property for each label
        source_key = self.LABEL_TO_ID_PROPERTY.get(source_label)
        target_key = self.LABEL_TO_ID_PROPERTY.get(target_label)

        if not source_key or not target_key:
            logger.warning(
                f"Unknown label: source={source_label}, target={target_label}"
            )
            return None

        # Build specs in MemgraphIngestor format
        from_spec = (source_label, source_key, rel.source_id)
        to_spec = (target_label, target_key, rel.target_id)

        # Convert properties from protobuf Struct to dict
        properties: dict[str, Any] = {}
        if rel.properties:
            for key, value in rel.properties.fields.items():
                # Extract value from protobuf Value
                if value.HasField("string_value"):
                    properties[key] = value.string_value
                elif value.HasField("number_value"):
                    properties[key] = value.number_value
                elif value.HasField("bool_value"):
                    properties[key] = value.bool_value

        return from_spec, rel_type_name, to_spec, properties

    def export_to_csv(self, output_dir: Path) -> tuple[dict[str, Path], Path]:
        """
        Export protobuf index to CSV files for fast LOAD CSV import.

        Creates one CSV file per node label in output_dir/nodes/ and a single
        relationships.csv file. CSV format is optimized for Memgraph's LOAD CSV.

        Args:
            output_dir: Directory to write CSV files to

        Returns:
            Tuple of (node_files_dict, relationships_path)
            - node_files_dict: {label: csv_path} mapping
            - relationships_path: Path to relationships.csv
        """
        # Load the index if not already loaded
        if not self._nodes:
            self.load()

        output_dir = Path(output_dir)
        nodes_dir = output_dir / "nodes"
        nodes_dir.mkdir(parents=True, exist_ok=True)

        # Group nodes by label
        nodes_by_label: dict[str, list[NodeProperties]] = defaultdict(list)
        for node in self._nodes:
            result = self.extract_node_data(node)
            if result:
                label, props = result
                # Convert list values to comma-separated strings for CSV
                csv_props = {}
                for key, value in props.items():
                    if isinstance(value, list):
                        csv_props[key] = ",".join(str(v) for v in value)
                    else:
                        csv_props[key] = value
                nodes_by_label[label].append(csv_props)

        # Write one CSV per label
        node_files: dict[str, Path] = {}
        for label, props_list in nodes_by_label.items():
            if not props_list:
                continue

            csv_path = nodes_dir / f"{label.lower()}.csv"

            # Collect all field names across all nodes of this label
            all_fields: set[str] = set()
            for props in props_list:
                all_fields.update(props.keys())

            fieldnames = sorted(all_fields)

            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for props in props_list:
                    writer.writerow(props)

            node_files[label] = csv_path
            logger.info(f"Exported {len(props_list)} {label} nodes to {csv_path}")

        # Write relationships CSV
        rels_path = output_dir / "relationships.csv"
        with open(rels_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["from_id", "to_id", "rel_type", "from_label", "to_label"])

            rel_count = 0
            for rel in self._relationships:
                result = self.extract_relationship_data(rel)
                if result:
                    from_spec, rel_type, to_spec, _ = result
                    from_label, _, from_id = from_spec
                    to_label, _, to_id = to_spec
                    writer.writerow([from_id, to_id, rel_type, from_label, to_label])
                    rel_count += 1

        logger.info(f"Exported {rel_count} relationships to {rels_path}")

        return node_files, rels_path
