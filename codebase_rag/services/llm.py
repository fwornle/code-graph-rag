from loguru import logger
from pydantic_ai import Agent, DeferredToolRequests, Tool

from ..config import settings
from ..prompts import (
    CYPHER_SYSTEM_PROMPT,
    LOCAL_CYPHER_SYSTEM_PROMPT,
    RAG_ORCHESTRATOR_SYSTEM_PROMPT,
)
from ..providers.base import get_provider


class LLMGenerationError(Exception):
    """Custom exception for LLM generation failures."""

    pass


def _clean_cypher_response(response_text: str) -> str:
    """Utility to clean up common LLM formatting artifacts from a Cypher query."""
    query = response_text.strip().replace("`", "")
    if query.startswith("cypher"):
        query = query[6:].strip()
    if not query.endswith(";"):
        query += ";"
    return query


class CypherGenerator:
    """Generates Cypher queries from natural language."""

    def __init__(self) -> None:
        try:
            config = settings.active_cypher_config

            provider = get_provider(
                config.provider,
                api_key=config.api_key,
                endpoint=config.endpoint,
                project_id=config.project_id,
                region=config.region,
                provider_type=config.provider_type,
                thinking_budget=config.thinking_budget,
            )

            llm = provider.create_model(config.model_id)

            system_prompt = (
                LOCAL_CYPHER_SYSTEM_PROMPT
                if config.provider == "ollama"
                else CYPHER_SYSTEM_PROMPT
            )

            self.agent = Agent(
                model=llm,
                system_prompt=system_prompt,
                output_type=str,
                retries=settings.AGENT_RETRIES,
            )
        except Exception as e:
            raise LLMGenerationError(
                f"Failed to initialize CypherGenerator: {e}"
            ) from e

    async def generate(self, natural_language_query: str) -> str:
        logger.info(
            f"  [CypherGenerator] Generating query for: '{natural_language_query}'"
        )
        try:
            result = await self.agent.run(natural_language_query)
            if (
                not isinstance(result.output, str)
                or "MATCH" not in result.output.upper()
            ):
                raise LLMGenerationError(
                    f"LLM did not generate a valid query. Output: {result.output}"
                )

            query = _clean_cypher_response(result.output)
            logger.info(f"  [CypherGenerator] Generated Cypher: {query}")
            return query
        except Exception as e:
            logger.error(f"  [CypherGenerator] Error: {e}")
            raise LLMGenerationError(f"Cypher generation failed: {e}") from e

    async def fix_query(
        self, original_query: str, error_message: str, original_request: str
    ) -> str:
        """Attempt to fix a failed Cypher query based on the error message."""
        fix_prompt = f"""The following Cypher query failed with an error. Please fix it.

ORIGINAL REQUEST: {original_request}

FAILED QUERY:
{original_query}

ERROR MESSAGE:
{error_message}

Generate ONLY a corrected Cypher query. Use simple, valid Cypher syntax.
Avoid complex patterns - prefer multiple simple MATCH clauses over complex path expressions.
Do NOT use variable-length paths like *1..3 unless absolutely necessary.
"""
        logger.info(f"  [CypherGenerator] Attempting to fix query after error: {error_message[:100]}...")
        try:
            result = await self.agent.run(fix_prompt)
            if not isinstance(result.output, str) or "MATCH" not in result.output.upper():
                raise LLMGenerationError(f"Fix attempt did not produce valid query: {result.output}")

            query = _clean_cypher_response(result.output)
            logger.info(f"  [CypherGenerator] Fixed Cypher: {query}")
            return query
        except Exception as e:
            logger.error(f"  [CypherGenerator] Fix attempt failed: {e}")
            raise LLMGenerationError(f"Query fix failed: {e}") from e


SYNTHESIS_SYSTEM_PROMPT = """You are a code analysis expert. Given source code,
documentation, and structural information, provide insightful analysis about:
- What the code does (purpose)
- How it's organized (components)
- Design patterns used
- Dependencies and coupling
- Potential issues or risks

Be concise but thorough. Focus on actionable insights.
Format your response with these exact headers:
PURPOSE: [1-2 sentences]
COMPONENTS: [comma-separated list]
PATTERNS: [comma-separated list]
DEPENDENCIES: [comma-separated list]
ISSUES: [comma-separated list, or "None identified"]
"""


def create_synthesis_agent() -> Agent:
    """Factory function to create the code synthesis/analysis agent."""
    try:
        config = settings.active_cypher_config  # Reuse cypher model config

        provider = get_provider(
            config.provider,
            api_key=config.api_key,
            endpoint=config.endpoint,
            project_id=config.project_id,
            region=config.region,
            provider_type=config.provider_type,
            thinking_budget=config.thinking_budget,
        )

        llm = provider.create_model(config.model_id)

        return Agent(
            model=llm,
            system_prompt=SYNTHESIS_SYSTEM_PROMPT,
            output_type=str,
            retries=settings.AGENT_RETRIES,
        )
    except Exception as e:
        raise LLMGenerationError(f"Failed to initialize Synthesis Agent: {e}") from e


def create_rag_orchestrator(tools: list[Tool]) -> Agent:
    """Factory function to create the main RAG orchestrator agent."""
    try:
        config = settings.active_orchestrator_config

        provider = get_provider(
            config.provider,
            api_key=config.api_key,
            endpoint=config.endpoint,
            project_id=config.project_id,
            region=config.region,
            provider_type=config.provider_type,
            thinking_budget=config.thinking_budget,
        )

        llm = provider.create_model(config.model_id)

        return Agent(
            model=llm,
            system_prompt=RAG_ORCHESTRATOR_SYSTEM_PROMPT,
            tools=tools,
            retries=settings.AGENT_RETRIES,
            output_retries=100,
            output_type=[str, DeferredToolRequests],
        )
    except Exception as e:
        raise LLMGenerationError(f"Failed to initialize RAG Orchestrator: {e}") from e
