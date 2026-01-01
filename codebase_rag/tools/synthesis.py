"""Comprehensive code analysis tool that synthesizes insights using LLM."""

from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field
from pydantic_ai import Agent, Tool

from ..services import QueryProtocol


class AnalysisSynthesis(BaseModel):
    """Result of comprehensive code analysis."""

    entity_name: str = Field(description="Fully qualified name of the analyzed entity")
    entity_type: str = Field(description="Type of entity (Function, Class, Method, Module)")
    purpose: str = Field(description="LLM-generated description of what the code does")
    components: list[str] = Field(
        default_factory=list, description="Key sub-components or methods"
    )
    patterns_identified: list[str] = Field(
        default_factory=list, description="Design patterns found"
    )
    dependencies: list[str] = Field(
        default_factory=list, description="What this entity depends on"
    )
    dependents: list[str] = Field(
        default_factory=list, description="What depends on this entity"
    )
    potential_issues: list[str] = Field(
        default_factory=list, description="Potential risks or concerns"
    )
    source_files: list[str] = Field(
        default_factory=list, description="Files that were analyzed"
    )
    documentation: str = Field(
        default="", description="Aggregated documentation and comments"
    )
    success: bool = Field(default=True, description="Whether analysis succeeded")
    error_message: str = Field(default="", description="Error message if analysis failed")


class ComprehensiveAnalyzer:
    """Performs comprehensive LLM-powered analysis of code entities.

    Chains multiple operations:
    1. Queries the knowledge graph for structure and relationships
    2. Reads source code from relevant files
    3. Aggregates documentation and comments
    4. Uses LLM to synthesize insights
    """

    def __init__(
        self,
        ingestor: QueryProtocol,
        project_root: str,
        synthesis_agent: Agent,
    ):
        self.ingestor = ingestor
        self.project_root = Path(project_root).resolve()
        self.synthesis_agent = synthesis_agent
        logger.info(f"ComprehensiveAnalyzer initialized with root: {self.project_root}")

    async def analyze(
        self,
        qualified_name: str,
        scope: str = "full",
    ) -> AnalysisSynthesis:
        """Perform comprehensive analysis of a code entity.

        Args:
            qualified_name: Full qualified name (e.g., 'project.module.ClassName')
            scope: Analysis depth - 'full', 'structure', 'behavior', 'dependencies'

        Returns:
            AnalysisSynthesis with comprehensive insights
        """
        logger.info(f"[ComprehensiveAnalyzer] Analyzing: {qualified_name} (scope: {scope})")

        try:
            # Step 1: Query graph for structure and type
            entity_info = await self._get_entity_info(qualified_name)
            if not entity_info:
                return AnalysisSynthesis(
                    entity_name=qualified_name,
                    entity_type="Unknown",
                    purpose="Entity not found in knowledge graph.",
                    success=False,
                    error_message=f"Entity '{qualified_name}' not found in the knowledge graph.",
                )

            entity_type = entity_info.get("type", "Unknown")

            # Step 2: Get relationships (callers, callees, inheritance)
            relationships = await self._get_relationships(qualified_name, entity_type)

            # Step 3: Collect source code
            source_code, source_files = await self._collect_source(
                qualified_name, entity_info, relationships
            )

            # Step 4: Aggregate documentation
            docs = self._aggregate_documentation(entity_info, relationships)

            # Step 5: LLM synthesis
            synthesis = await self._synthesize(
                qualified_name,
                entity_type,
                entity_info,
                relationships,
                source_code,
                docs,
                scope,
            )

            synthesis.source_files = source_files
            return synthesis

        except Exception as e:
            logger.error(f"[ComprehensiveAnalyzer] Error analyzing {qualified_name}: {e}")
            return AnalysisSynthesis(
                entity_name=qualified_name,
                entity_type="Unknown",
                purpose="Analysis failed due to an error.",
                success=False,
                error_message=str(e),
            )

    async def _get_entity_info(self, qualified_name: str) -> dict[str, Any] | None:
        """Query the graph for basic entity information."""
        query = """
            MATCH (n) WHERE n.qualified_name = $qn
            OPTIONAL MATCH (m:Module)-[*]-(n)
            RETURN
                labels(n)[0] AS type,
                n.name AS name,
                n.docstring AS docstring,
                n.comments AS comments,
                n.start_line AS start_line,
                n.end_line AS end_line,
                m.path AS file_path
            LIMIT 1
        """
        try:
            results = self.ingestor.fetch_all(query, {"qn": qualified_name})
            if results:
                return results[0]
        except Exception as e:
            logger.warning(f"[ComprehensiveAnalyzer] Error fetching entity info: {e}")
        return None

    async def _get_relationships(
        self, qualified_name: str, entity_type: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Get related entities (calls, called_by, inherits, children)."""
        relationships: dict[str, list[dict[str, Any]]] = {
            "calls": [],
            "called_by": [],
            "inherits": [],
            "inherited_by": [],
            "contains": [],
        }

        # Get what this entity calls
        calls_query = """
            MATCH (n)-[:CALLS]->(target)
            WHERE n.qualified_name = $qn
            RETURN target.qualified_name AS qn, labels(target)[0] AS type, target.docstring AS docstring
            LIMIT 20
        """
        try:
            results = self.ingestor.fetch_all(calls_query, {"qn": qualified_name})
            relationships["calls"] = results or []
        except Exception as e:
            logger.debug(f"Error fetching calls: {e}")

        # Get what calls this entity
        called_by_query = """
            MATCH (caller)-[:CALLS]->(n)
            WHERE n.qualified_name = $qn
            RETURN caller.qualified_name AS qn, labels(caller)[0] AS type, caller.docstring AS docstring
            LIMIT 20
        """
        try:
            results = self.ingestor.fetch_all(called_by_query, {"qn": qualified_name})
            relationships["called_by"] = results or []
        except Exception as e:
            logger.debug(f"Error fetching callers: {e}")

        # Get inheritance (if class)
        if entity_type in ("Class", "Interface"):
            inherits_query = """
                MATCH (n)-[:INHERITS]->(parent)
                WHERE n.qualified_name = $qn
                RETURN parent.qualified_name AS qn, labels(parent)[0] AS type, parent.docstring AS docstring
            """
            try:
                results = self.ingestor.fetch_all(inherits_query, {"qn": qualified_name})
                relationships["inherits"] = results or []
            except Exception as e:
                logger.debug(f"Error fetching inheritance: {e}")

            # Get what inherits from this class
            inherited_by_query = """
                MATCH (child)-[:INHERITS]->(n)
                WHERE n.qualified_name = $qn
                RETURN child.qualified_name AS qn, labels(child)[0] AS type
                LIMIT 20
            """
            try:
                results = self.ingestor.fetch_all(inherited_by_query, {"qn": qualified_name})
                relationships["inherited_by"] = results or []
            except Exception as e:
                logger.debug(f"Error fetching children: {e}")

            # Get methods for classes
            contains_query = """
                MATCH (n)-[:DEFINES_METHOD]->(method)
                WHERE n.qualified_name = $qn
                RETURN method.qualified_name AS qn, method.name AS name, method.docstring AS docstring
                LIMIT 30
            """
            try:
                results = self.ingestor.fetch_all(contains_query, {"qn": qualified_name})
                relationships["contains"] = results or []
            except Exception as e:
                logger.debug(f"Error fetching methods: {e}")

        return relationships

    async def _collect_source(
        self,
        qualified_name: str,
        entity_info: dict[str, Any],
        relationships: dict[str, list[dict[str, Any]]],
    ) -> tuple[str, list[str]]:
        """Collect source code for the entity and related items."""
        source_parts = []
        source_files = []

        # Read main entity source
        file_path_str = entity_info.get("file_path")
        start_line = entity_info.get("start_line")
        end_line = entity_info.get("end_line")

        if file_path_str and start_line and end_line:
            try:
                full_path = self.project_root / file_path_str
                if full_path.exists():
                    with full_path.open("r", encoding="utf-8") as f:
                        all_lines = f.readlines()
                    snippet = "".join(all_lines[start_line - 1 : end_line])
                    source_parts.append(f"# {qualified_name}\n{snippet}")
                    if file_path_str not in source_files:
                        source_files.append(file_path_str)
            except Exception as e:
                logger.debug(f"Error reading source for {qualified_name}: {e}")

        return "\n\n".join(source_parts), source_files

    def _aggregate_documentation(
        self,
        entity_info: dict[str, Any],
        relationships: dict[str, list[dict[str, Any]]],
    ) -> str:
        """Aggregate all documentation and comments."""
        docs_parts = []

        # Main entity docs
        if entity_info.get("docstring"):
            docs_parts.append(f"Docstring: {entity_info['docstring']}")
        if entity_info.get("comments"):
            docs_parts.append(f"Comments: {entity_info['comments']}")

        # Related entity docs (brief summaries)
        for method in relationships.get("contains", [])[:10]:
            if method.get("docstring"):
                docs_parts.append(f"  - {method.get('name')}: {method['docstring'][:100]}")

        return "\n".join(docs_parts)

    async def _synthesize(
        self,
        qualified_name: str,
        entity_type: str,
        entity_info: dict[str, Any],
        relationships: dict[str, list[dict[str, Any]]],
        source_code: str,
        docs: str,
        scope: str,
    ) -> AnalysisSynthesis:
        """Use LLM to generate comprehensive analysis."""

        # Build relationship summaries
        calls_summary = ", ".join([r.get("qn", "") for r in relationships["calls"][:10]])
        called_by_summary = ", ".join([r.get("qn", "") for r in relationships["called_by"][:10]])
        inherits_summary = ", ".join([r.get("qn", "") for r in relationships["inherits"]])
        contains_summary = ", ".join([r.get("name", "") for r in relationships["contains"][:15]])

        prompt = f"""Analyze this code entity and provide insights in a structured format.

ENTITY: {qualified_name}
TYPE: {entity_type}

DOCUMENTATION:
{docs if docs else "No documentation available"}

SOURCE CODE (truncated if large):
{source_code[:3000] if source_code else "Source not available"}

RELATIONSHIPS:
- Calls: {calls_summary if calls_summary else "None"}
- Called by: {called_by_summary if called_by_summary else "None"}
- Inherits from: {inherits_summary if inherits_summary else "None"}
- Contains (methods/functions): {contains_summary if contains_summary else "None"}

ANALYSIS SCOPE: {scope}

Provide your analysis in the following format:
PURPOSE: [1-2 sentences describing what this code does]
COMPONENTS: [comma-separated list of key sub-components or methods]
PATTERNS: [comma-separated list of design patterns used]
DEPENDENCIES: [comma-separated list of critical external dependencies]
ISSUES: [comma-separated list of potential risks or concerns, or "None identified"]
"""

        try:
            result = await self.synthesis_agent.run(prompt)
            return self._parse_synthesis_response(
                qualified_name, entity_type, docs, result.output
            )
        except Exception as e:
            logger.error(f"[ComprehensiveAnalyzer] Synthesis failed: {e}")
            # Return basic info without LLM synthesis
            return AnalysisSynthesis(
                entity_name=qualified_name,
                entity_type=entity_type,
                purpose=docs[:200] if docs else "LLM synthesis failed",
                dependencies=[r.get("qn", "") for r in relationships["calls"][:5]],
                dependents=[r.get("qn", "") for r in relationships["called_by"][:5]],
                documentation=docs,
            )

    def _parse_synthesis_response(
        self,
        qualified_name: str,
        entity_type: str,
        docs: str,
        response: str,
    ) -> AnalysisSynthesis:
        """Parse the LLM response into structured AnalysisSynthesis."""
        result = AnalysisSynthesis(
            entity_name=qualified_name,
            entity_type=entity_type,
            purpose="",
            documentation=docs,
        )

        lines = response.strip().split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("PURPOSE:"):
                result.purpose = line[8:].strip()
            elif line.startswith("COMPONENTS:"):
                items = line[11:].strip()
                result.components = [c.strip() for c in items.split(",") if c.strip()]
            elif line.startswith("PATTERNS:"):
                items = line[9:].strip()
                result.patterns_identified = [p.strip() for p in items.split(",") if p.strip()]
            elif line.startswith("DEPENDENCIES:"):
                items = line[13:].strip()
                result.dependencies = [d.strip() for d in items.split(",") if d.strip()]
            elif line.startswith("ISSUES:"):
                items = line[7:].strip()
                if items.lower() != "none identified":
                    result.potential_issues = [i.strip() for i in items.split(",") if i.strip()]

        # If purpose wasn't parsed, use the whole response
        if not result.purpose:
            result.purpose = response[:500]

        return result


def create_comprehensive_analysis_tool(analyzer: ComprehensiveAnalyzer) -> Tool:
    """Factory function to create the comprehensive analysis tool."""

    async def comprehensive_code_analysis(
        qualified_name: str,
        scope: str = "full",
    ) -> AnalysisSynthesis:
        """
        Performs comprehensive LLM-powered analysis of a code entity.

        Chains multiple tools internally:
        1. Queries the knowledge graph for structure and relationships
        2. Reads source code from relevant files
        3. Aggregates documentation and comments
        4. Uses LLM to synthesize insights

        Args:
            qualified_name: Full qualified name (e.g., 'project.module.ClassName')
            scope: Analysis depth - 'full', 'structure', 'behavior', 'dependencies'

        Returns:
            Comprehensive analysis including purpose, patterns, dependencies, and risks.
        """
        logger.info(f"[Tool:ComprehensiveAnalysis] Analyzing: {qualified_name}")
        return await analyzer.analyze(qualified_name, scope)

    return Tool(
        function=comprehensive_code_analysis,
        description="Comprehensive LLM-powered code analysis that queries the graph, reads source files, and synthesizes insights about purpose, patterns, dependencies, and risks.",
    )
