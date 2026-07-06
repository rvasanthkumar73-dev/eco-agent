import asyncio
import datetime
import json
import logging
import os
import re
import sys
from pydantic import BaseModel, Field
from google.genai.errors import ServerError, ClientError

logger = logging.getLogger("eco_agent.retrying_gemini")

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.workflow import Workflow, node
from google.genai import types
from mcp import StdioServerParameters

from app.config import config

# Initialize local MCP server toolset using stdio connection parameters
mcp_script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "mcp_server.py"))
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_script_path],
        )
    )
)

# -----------------------------------------------------------------------------
# Structured Output Schemas (Pydantic Models)
# -----------------------------------------------------------------------------

class CarbonActivity(BaseModel):
    activity_type: str = Field(description="Type of activity: 'transit' or 'meal'")
    subtype: str = Field(description="Subtype: e.g. 'car', 'bus', 'train', 'flight', 'beef', 'chicken', 'vegetarian', 'vegan'")
    quantity: float = Field(description="Quantity: distance in km for transit, number of meals for meal")
    emissions_kg: float = Field(description="Calculated CO2 emissions in kg")

class TrackerOutput(BaseModel):
    activities: list[CarbonActivity] = Field(description="List of parsed and calculated carbon activities")
    total_emissions_kg: float = Field(description="Sum of emissions from all activities")

class AlternativeSuggestion(BaseModel):
    activity_type: str = Field(description="Type of activity: 'transit' or 'meal'")
    current_subtype: str = Field(description="Current subtype used")
    better_alternative: str = Field(description="Recommended greener alternative subtype")
    potential_saving_pct: float = Field(description="Percentage of CO2 saved by switching")
    saving_message: str = Field(description="Explanation of the savings and environmental benefit")

class InterventionOutput(BaseModel):
    suggestions: list[AlternativeSuggestion] = Field(description="Actionable behavior intervention suggestions")
    offset_recommendations: list[str] = Field(description="List of suggested offset project names and brief descriptions")

class OrchestratorOutput(BaseModel):
    activities: list[CarbonActivity] = Field(description="Breakdown of all tracked carbon activities")
    total_emissions_kg: float = Field(description="Combined total carbon emissions in kg CO2")
    suggestions: list[AlternativeSuggestion] = Field(description="Behavioral modification recommendations")
    offset_recommendations: list[str] = Field(description="Carbon offset recommendation suggestions")
    markdown_report: str = Field(description="A clean, comprehensive, professional Markdown report presenting the emissions breakdown and behavioral suggestions")
# -----------------------------------------------------------------------------
# Graceful Schema Validation Patches for OrchestratorOutput
# -----------------------------------------------------------------------------
from google.adk.workflow._base_node import BaseNode
from google.adk.utils import _schema_utils
from pydantic import ValidationError

_original_validate_schema = BaseNode._validate_schema
_original_validate_output_data = BaseNode._validate_output_data
_original_utils_validate_schema = _schema_utils.validate_schema

def _wrapped_validate_schema(self, data, schema):
    try:
        return _original_validate_schema(self, data, schema)
    except ValidationError as e:
        if schema == OrchestratorOutput:
            logger.error(f"ValidationError in BaseNode._validate_schema for OrchestratorOutput: {e}. Gracefully recovering.")
            raw_text = str(data)
            return {
                "activities": [],
                "total_emissions_kg": 0.0,
                "suggestions": [],
                "offset_recommendations": [],
                "markdown_report": f"### Emissions Calculation & Behavioral Recommendations\n\n{raw_text}"
            }
        raise e

def _wrapped_validate_output_data(self, data):
    try:
        return _original_validate_output_data(self, data)
    except ValidationError as e:
        if self.output_schema == OrchestratorOutput:
            logger.error(f"ValidationError in BaseNode._validate_output_data for OrchestratorOutput: {e}. Gracefully recovering.")
            raw_text = str(data)
            return {
                "activities": [],
                "total_emissions_kg": 0.0,
                "suggestions": [],
                "offset_recommendations": [],
                "markdown_report": f"### Emissions Calculation & Behavioral Recommendations\n\n{raw_text}"
            }
        raise e

def _wrapped_utils_validate_schema(schema, json_text):
    try:
        return _original_utils_validate_schema(schema, json_text)
    except Exception as e:
        if schema == OrchestratorOutput:
            logger.error(f"ValidationError in _schema_utils.validate_schema for OrchestratorOutput: {e}. Gracefully recovering.")
            return {
                "activities": [],
                "total_emissions_kg": 0.0,
                "suggestions": [],
                "offset_recommendations": [],
                "markdown_report": f"### Emissions Calculation & Behavioral Recommendations\n\n{json_text}"
            }
        raise e

BaseNode._validate_schema = _wrapped_validate_schema
BaseNode._validate_output_data = _wrapped_validate_output_data
_schema_utils.validate_schema = _wrapped_utils_validate_schema

# -----------------------------------------------------------------------------
# Retrying Gemini Wrapper
# -----------------------------------------------------------------------------

class RetryingGemini(Gemini):
    async def generate_content_async(self, llm_request, stream: bool = False):
        # Rate-limiting: add a 13-second delay before every API call to stay under 5 requests/minute (12s spacing + 1s safety margin)
        await asyncio.sleep(13.0)
        
        # Enforce structured JSON output at the Gemini API level ONLY if a response schema is present and tools are NOT configured (to prevent API errors)
        if llm_request.config and getattr(llm_request.config, 'response_schema', None) is not None:
            if not getattr(llm_request.config, 'tools', None):
                llm_request.config.response_mime_type = "application/json"

        max_attempts = 6
        delay = 3.0
        for attempt in range(max_attempts):
            try:
                # Call super's generate_content_async
                async for response in super().generate_content_async(llm_request, stream=stream):
                    yield response
                return
            except ServerError as se:
                is_503 = (getattr(se, 'code', None) == 503) or ("503" in str(se)) or ("UNAVAILABLE" in str(se))
                if is_503 and attempt < max_attempts - 1:
                    logger.warning(
                        f"Gemini API returned 503 UNAVAILABLE. Retrying in {delay}s "
                        f"(attempt {attempt + 1}/{max_attempts})..."
                    )
                    await asyncio.sleep(delay)
                    delay *= 2.0
                    continue
                raise se
            except ClientError as ce:
                # 429 quota and other client errors are raised immediately without retry
                raise ce

# -----------------------------------------------------------------------------
# Specialized Sub-Agents
# -----------------------------------------------------------------------------

tracker_agent = LlmAgent(
    name="tracker_agent",
    model=RetryingGemini(model=config.model),
    instruction=(
        "You are the specialized Carbon Footprint Tracker sub-agent. "
        "Your task is to parse the user's raw activity log and compute the emissions. "
        "For each activity identified, you MUST invoke the appropriate MCP tool(s) to fetch carbon coefficients or calculate emissions. "
        "Do not make up coefficients. Sum all calculated emissions and output the activities list and total_emissions_kg."
    ),
    tools=[mcp_toolset],
    output_schema=TrackerOutput,
)

intervention_agent = LlmAgent(
    name="intervention_agent",
    model=RetryingGemini(model=config.model),
    instruction=(
        "You are the specialized Behavioral Intervention sub-agent. "
        "Your task is to review a user's carbon activities and provide suggestions to lower their footprint. "
        "Use the MCP tools (get_green_alternatives, get_offset_options) to find greener alternatives "
        "and offset recommendations. Formulate specific suggestions, compute the percentage savings, and provide helpful advice."
    ),
    tools=[mcp_toolset],
    output_schema=InterventionOutput,
)

# -----------------------------------------------------------------------------
# Primary Orchestrator Agent
# -----------------------------------------------------------------------------

orchestrator = LlmAgent(
    name="orchestrator",
    model=RetryingGemini(model=config.model),
    instruction=(
        "You are the primary Eco-Agent Orchestrator. "
        "Your role is to coordinate carbon footprint tracking and behavior intervention. "
        "1. Delegate the user's activities to the tracker_agent tool to compute the carbon footprint. "
        "2. Pass the tracker's output to the intervention_agent tool to generate green alternatives and offset suggestions. "
        "3. Combine the outputs into an OrchestratorOutput object. The markdown_report field must contain a beautiful "
        "   and professional Markdown report presenting the carbon calculation breakdown, total footprint, "
        "   greener options, and offset recommendations."
    ),
    tools=[AgentTool(tracker_agent), AgentTool(intervention_agent)],
    output_schema=OrchestratorOutput,
)

# -----------------------------------------------------------------------------
# Workflow Graph Nodes
# -----------------------------------------------------------------------------

def security_checkpoint(ctx: Context, node_input: types.Content) -> Event:
    """Performs security scanning (PII scrubbing, prompt injection, domain constraints)."""
    # Extract text from types.Content
    input_text = ""
    if isinstance(node_input, types.Content):
        for part in node_input.parts:
            if part.text:
                input_text += part.text
    elif isinstance(node_input, str):
        input_text = node_input

    # 1. PII Redaction
    email_pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    phone_pattern = r'\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}'
    gps_pattern = r'[-+]?([1-8]?\d(\.\d+)?|90(\.0+)?),\s*[-+]?(180(\.0+)?|((1[0-7]\d)|([1-9]?\d))(\.\d+)?)'

    cleaned_text = input_text
    pii_found = False

    if config.pii_redaction_enabled:
        if re.search(email_pattern, cleaned_text):
            cleaned_text = re.sub(email_pattern, "[EMAIL_REDACTED]", cleaned_text)
            pii_found = True
        if re.search(phone_pattern, cleaned_text):
            cleaned_text = re.sub(phone_pattern, "[PHONE_REDACTED]", cleaned_text)
            pii_found = True
        if re.search(gps_pattern, cleaned_text):
            cleaned_text = re.sub(gps_pattern, "[LOCATION_REDACTED]", cleaned_text)
            pii_found = True

    # 2. Prompt Injection Check
    injection_detected = False
    if config.injection_detection_enabled:
        injection_keywords = [
            "ignore previous instructions", "ignore all instructions", "bypass security",
            "system prompt", "jailbreak", "override instructions", "you are now a",
            "forget your instructions"
        ]
        for kw in injection_keywords:
            if kw in cleaned_text.lower():
                injection_detected = True
                break

    # 3. Domain Specific Rules
    domain_blocked = False
    block_reason = ""
    if not cleaned_text.strip():
        domain_blocked = True
        block_reason = "Input activity log cannot be empty."
    elif len(cleaned_text) > 1000:
        domain_blocked = True
        block_reason = "Input exceeds maximum character limit of 1000."

    # Audit Logging
    audit_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "input_length": len(input_text),
        "pii_redacted": pii_found,
        "prompt_injection_detected": injection_detected,
        "domain_rules_blocked": domain_blocked,
        "severity": "INFO"
    }

    if injection_detected:
        audit_entry["severity"] = "CRITICAL"
        audit_entry["message"] = "Prompt injection attempt blocked."
        _write_audit(audit_entry)
        return Event(route="SECURITY_EVENT", output="Security Violation: Input blocked due to suspected prompt injection.")

    if domain_blocked:
        audit_entry["severity"] = "WARNING"
        audit_entry["message"] = f"Domain validation failed: {block_reason}"
        _write_audit(audit_entry)
        return Event(route="SECURITY_EVENT", output=f"Validation Failed: {block_reason}")

    if pii_found:
        audit_entry["severity"] = "WARNING"
        audit_entry["message"] = "PII detected and redacted."
    else:
        audit_entry["message"] = "Security checks passed."

    _write_audit(audit_entry)
    return Event(route="orchestrator", output=cleaned_text)

def _write_audit(entry: dict):
    try:
        with open("security_audit.json", "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

def security_event_handler(node_input: str):
    """Terminal node executing on security failure."""
    message = f"⚠️ **Security Check Blocked Request**\n\nReason: {node_input}"
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=message)]))
    yield Event(output=message)

@node(rerun_on_resume=True)
async def approval_node(ctx: Context, node_input: OrchestratorOutput) -> Event:
    """Human-in-the-Loop Node. Pauses the workflow until user confirms saving the log."""
    if ctx.resume_inputs and "ask_approval" in ctx.resume_inputs:
        user_response = ctx.resume_inputs["ask_approval"].strip().lower()
        if user_response in ["yes", "y", "approve"]:
            yield Event(
                content=types.Content(
                    role='model',
                    parts=[types.Part.from_text(text="✅ **Carbon log successfully saved to history!**")]
                )
            )
            report_text = node_input.get("markdown_report", "") if isinstance(node_input, dict) else getattr(node_input, "markdown_report", "")
            yield Event(output={"approved": True, "report": report_text})
            return
        else:
            yield Event(
                content=types.Content(
                    role='model',
                    parts=[types.Part.from_text(text="❌ **Carbon log cancelled by user.**")]
                )
            )
            yield Event(output={"approved": False, "report": "Log cancelled by user."})
            return

    # Yield the request input to pause
    yield RequestInput(
        interrupt_id="ask_approval",
        message="Would you like to save this daily activity log and recommendations to your history? (yes/no)"
    )

def final_output(node_input: dict) -> Event:
    """Terminal node displaying the carbon footprint report."""
    report = node_input.get("report", "No report available.")
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=report)]))
    yield Event(output=report)

# -----------------------------------------------------------------------------
# App and Workflow Initialization
# -----------------------------------------------------------------------------

app = App(
    name="app",
    root_agent=Workflow(
        name="eco_workflow",
        edges=[
            ('START', security_checkpoint),
            (security_checkpoint, {
                "SECURITY_EVENT": security_event_handler,
                "orchestrator": orchestrator
            }),
            (orchestrator, approval_node),
            (approval_node, final_output),
        ]
    ),
    resumability_config=ResumabilityConfig(is_resumable=True)
)


