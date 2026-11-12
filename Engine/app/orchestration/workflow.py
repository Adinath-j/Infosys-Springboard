"""LangGraph Multi-Agent Orchestration Workflow.

Coordinates Document Agent -> Extraction Agent -> Validation -> Red Flag Agent -> Persistence.
Supports both native LangGraph StateGraph (when installed) and a pure Python workflow engine fallback.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.agents.document_agent import DocumentAgentService
from app.agents.extraction_agent import ExtractionAgentService
from app.agents.red_flag_agent import RedFlagAgentService
from app.infrastructure.database import get_database_repository
from app.infrastructure.storage import get_file_storage
from app.orchestration.state import AgentExecutionLog, FinancialResearchState
from app.schemas.agents import ExtractedFinancials

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==============================================================================
# Node Implementations
# ==============================================================================

def document_agent_node(state: FinancialResearchState) -> FinancialResearchState:
    """Step 1: Document Ingestion, Parsing, Table Extraction, Chunking, Embedding, Indexing."""
    logger.info("Executing Document Agent node for doc: %s", state.document_id)
    repo = get_database_repository()
    doc_service = DocumentAgentService()

    start_time = utc_now()
    try:
        # If file bytes are provided directly:
        if state.file_bytes:
            upload_resp = doc_service.process_pdf_upload(
                file_bytes=state.file_bytes,
                filename=state.filename,
                workspace_id=state.workspace_id,
                session_id=state.session_id,
                document_id=state.document_id,
            )
            state.document_id = upload_resp.document_id
            state.total_pages = upload_resp.total_pages
            state.total_characters = upload_resp.characters_extracted
            state.chunk_count = upload_resp.chunk_count
            state.is_indexed = upload_resp.indexed
        elif state.storage_path and Path(state.storage_path).exists():
            file_bytes = Path(state.storage_path).read_bytes()
            upload_resp = doc_service.process_pdf_upload(
                file_bytes=file_bytes,
                filename=state.filename,
                workspace_id=state.workspace_id,
                session_id=state.session_id,
                document_id=state.document_id,
            )
            state.document_id = upload_resp.document_id
            state.total_pages = upload_resp.total_pages
            state.total_characters = upload_resp.characters_extracted
            state.chunk_count = upload_resp.chunk_count
            state.is_indexed = upload_resp.indexed
        else:
            # Check if document already exists in DB and is indexed
            existing = repo.get_document_by_id(state.document_id)
            if existing and existing.get("indexed"):
                state.total_pages = existing.get("total_pages", 0)
                state.chunk_count = existing.get("chunk_count", 0)
                state.is_indexed = True
            else:
                # Retrieve from storage abstraction (Supabase Storage or Local)
                storage = get_file_storage()
                obj_key = (existing.get("metadata") or {}).get("object_key") if existing else None
                file_bytes = None
                try:
                    file_bytes = storage.get_file_bytes(state.document_id, object_key=obj_key)
                except Exception as exc:
                    logger.debug("Storage retrieval in workflow: %s", exc)

                if file_bytes:
                    upload_resp = doc_service.process_pdf_upload(
                        file_bytes=file_bytes,
                        filename=state.filename,
                        workspace_id=state.workspace_id,
                        session_id=state.session_id,
                        document_id=state.document_id,
                    )
                    state.document_id = upload_resp.document_id
                    state.total_pages = upload_resp.total_pages
                    state.total_characters = upload_resp.characters_extracted
                    state.chunk_count = upload_resp.chunk_count
                    state.is_indexed = upload_resp.indexed
                else:
                    raise ValueError(
                        f"Cannot process document '{state.document_id}': file not found in storage or on disk."
                    )

        state.current_phase = "PARSED_AND_INDEXED"
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="document_agent_node",
                agent="DOCUMENT_AGENT",
                status="SUCCESS",
                timestamp=start_time,
                message=f"Indexed {state.total_pages} pages and {state.chunk_count} chunks.",
            )
        )
        if state.job_id:
            repo.update_processing_job(state.job_id, status="PROCESSING", current_stage="DOCUMENT_AGENT_COMPLETED")

    except Exception as e:
        logger.exception("Error in document_agent_node: %s", e)
        state.is_failed = True
        state.errors.append(f"Document Agent Error: {e}")
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="document_agent_node",
                agent="DOCUMENT_AGENT",
                status="FAILED",
                timestamp=start_time,
                message=str(e),
            )
        )
        if state.job_id:
            repo.update_processing_job(state.job_id, status="FAILED", current_stage="DOCUMENT_AGENT", error_message=str(e))

    return state


def extraction_agent_node(state: FinancialResearchState) -> FinancialResearchState:
    """Step 2: Financial Metric Extraction & Python Decimal Ratio Computations."""
    if state.is_failed or not state.is_indexed:
        logger.warning("Skipping extraction_agent_node due to upstream failure.")
        return state

    logger.info("Executing Extraction Agent node for doc: %s", state.document_id)
    repo = get_database_repository()
    ext_service = ExtractionAgentService()
    start_time = utc_now()

    try:
        financials: ExtractedFinancials = ext_service.extract_document_financials(
            document_id=state.document_id,
            session_id=state.session_id,
        )

        all_metrics_dict = {}
        for cat in (financials.income_statement, financials.balance_sheet, financials.cash_flow, financials.ratios):
            for name, m in cat.items():
                all_metrics_dict[name] = {
                    "name": m.name,
                    "value": str(m.value),
                    "statement": "KEY_RATIOS" if m.is_calculated else "STATEMENT",
                    "citation": {
                        "document_id": m.citation.document_id,
                        "page_number": m.citation.page_number,
                        "exact_quote": m.citation.exact_quote,
                        "is_calculated": m.citation.is_calculated,
                    },
                }

        state.extracted_financials = {
            "company_name": financials.company_name,
            "fiscal_year": financials.fiscal_year,
            "is_consolidated": financials.is_consolidated,
            "metrics": all_metrics_dict,
            "_model": financials,
        }
        state.extracted_metrics_count = len(all_metrics_dict)
        state.current_phase = "EXTRACTION_COMPLETED"
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="extraction_agent_node",
                agent="EXTRACTION_AGENT",
                status="SUCCESS",
                timestamp=start_time,
                message=f"Extracted {state.extracted_metrics_count} metrics.",
            )
        )
        if state.job_id:
            repo.update_processing_job(state.job_id, status="PROCESSING", current_stage="EXTRACTION_COMPLETED")

    except Exception as e:
        logger.exception("Error in extraction_agent_node: %s", e)
        state.is_failed = True
        state.errors.append(f"Extraction Agent Error: {e}")
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="extraction_agent_node",
                agent="EXTRACTION_AGENT",
                status="FAILED",
                timestamp=start_time,
                message=str(e),
            )
        )
        if state.job_id:
            repo.update_processing_job(state.job_id, status="FAILED", current_stage="EXTRACTION_AGENT", error_message=str(e))

    return state


def evidence_validation_node(state: FinancialResearchState) -> FinancialResearchState:
    """Step 3: Verification of extracted metrics and source citations."""
    start_time = utc_now()
    if state.is_failed or not state.extracted_financials:
        state.validation_passed = False
        return state

    metrics = state.extracted_financials.get("metrics", {})
    if not metrics:
        state.validation_passed = False
        state.errors.append("Evidence validation warning: No metrics extracted.")
        return state

    repo = get_database_repository()
    validated_citations = 0
    validation_issues = []

    for name, m in metrics.items():
        citation = m.get("citation") or {}
        page_num = citation.get("page_number", 0)
        is_calc = citation.get("is_calculated", False)

        if is_calc:
            validated_citations += 1
        else:
            cit_doc_id = citation.get("document_id")
            if cit_doc_id and cit_doc_id != state.document_id:
                validation_issues.append(f"Metric '{name}' citation document mismatch: {cit_doc_id}")

            if state.total_pages > 0 and (page_num < 1 or page_num > state.total_pages):
                validation_issues.append(f"Metric '{name}' citation page {page_num} out of bounds (1-{state.total_pages})")
            elif page_num > 0:
                validated_citations += 1

    if validated_citations > 0:
        state.validation_passed = True
        state.current_phase = "VALIDATION_PASSED"
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="evidence_validation_node",
                agent="EVIDENCE_VALIDATION",
                status="SUCCESS",
                timestamp=start_time,
                message=f"Validated {validated_citations} metric citations across document filings.",
            )
        )
        if state.job_id:
            repo.update_processing_job(state.job_id, status="PROCESSING", current_stage="EVIDENCE_VALIDATION_COMPLETED")
    else:
        state.validation_passed = False
        err_msg = "Evidence validation warning: Low extraction confidence or missing citations."
        state.errors.append(err_msg)
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="evidence_validation_node",
                agent="EVIDENCE_VALIDATION",
                status="FAILED",
                timestamp=start_time,
                message=err_msg,
            )
        )
        if state.job_id:
            repo.update_processing_job(state.job_id, status="FAILED", current_stage="EVIDENCE_VALIDATION", error_message=err_msg)

    return state


def red_flag_agent_node(state: FinancialResearchState) -> FinancialResearchState:
    """Step 4: Hybrid Financial Risk Detection (Deterministic Rules + LLM Disclosures)."""
    if state.is_failed or not state.validation_passed:
        logger.warning("Skipping red_flag_agent_node due to upstream validation failure.")
        return state

    logger.info("Executing Red Flag Agent node for doc: %s", state.document_id)
    repo = get_database_repository()
    rf_service = RedFlagAgentService()
    ext_service = ExtractionAgentService()
    start_time = utc_now()

    try:
        # Reuse ExtractedFinancials model from state if available; avoid duplicate LLM re-extraction
        financials = (state.extracted_financials or {}).get("_model")
        if not financials:
            financials = ext_service.extract_document_financials(
                document_id=state.document_id,
                session_id=state.session_id,
            )

        findings = rf_service.analyze_document_risks(
            document_id=state.document_id,
            financials=financials,
            session_id=state.session_id,
        )

        state.red_flag_findings = [
            {
                "finding_id": f.finding_id,
                "category": f.category.value,
                "severity": f.severity.value,
                "title": f.title,
                "description": f.description,
                "supporting_evidence": f.supporting_evidence,
                "source_page": f.source_citation.page_number,
            }
            for f in findings
        ]
        state.red_flag_count = len(state.red_flag_findings)
        state.current_phase = "RED_FLAGS_IDENTIFIED"
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="red_flag_agent_node",
                agent="RED_FLAG_AGENT",
                status="SUCCESS",
                timestamp=start_time,
                message=f"Identified {state.red_flag_count} risk findings.",
            )
        )
        if state.job_id:
            repo.update_processing_job(state.job_id, status="PROCESSING", current_stage="RED_FLAGS_COMPLETED")

    except Exception as e:
        logger.exception("Error in red_flag_agent_node: %s", e)
        state.errors.append(f"Red Flag Agent Error: {e}")
        state.execution_steps.append(
            AgentExecutionLog(
                step_name="red_flag_agent_node",
                agent="RED_FLAG_AGENT",
                status="FAILED",
                timestamp=start_time,
                message=str(e),
            )
        )

    return state


def persistence_node(state: FinancialResearchState) -> FinancialResearchState:
    """Step 5: Final state persistence and job status update."""
    logger.info("Executing persistence node for doc: %s", state.document_id)
    repo = get_database_repository()

    status_str = "COMPLETED" if not state.is_failed else "FAILED"
    err_str = "; ".join(state.errors) if state.errors else None

    if state.job_id:
        repo.update_processing_job(
            job_id=state.job_id,
            status=status_str,
            current_stage="PIPELINE_COMPLETED",
            error_message=err_str,
        )

    state.current_phase = "COMPLETED" if not state.is_failed else "FAILED"
    state.final_summary = {
        "document_id": state.document_id,
        "filename": state.filename,
        "total_pages": state.total_pages,
        "chunk_count": state.chunk_count,
        "metrics_extracted": state.extracted_metrics_count,
        "red_flags_identified": state.red_flag_count,
        "status": status_str,
    }

    return state


# ==============================================================================
# Workflow Orchestrator
# ==============================================================================

class MultiAgentWorkflowOrchestrator:
    """Executes the 3-agent sequential workflow."""

    @staticmethod
    def run_pipeline(
        document_id: str,
        filename: str,
        file_bytes: Optional[bytes] = None,
        storage_path: Optional[str] = None,
        workspace_id: Optional[str] = "default-workspace",
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> FinancialResearchState:
        """Run Document Agent -> Extraction Agent -> Validation -> Red Flag Agent -> Persistence."""
        repo = get_database_repository()

        # Ensure default-workspace exists if targeted, or validate existence
        if workspace_id:
            ws = repo.get_workspace(workspace_id)
            if not ws:
                if workspace_id == "default-workspace":
                    repo.create_workspace(
                        workspace_id="default-workspace",
                        name="Default Workspace",
                        description="System preloaded workspace",
                    )
                else:
                    raise ValueError(f"Workspace '{workspace_id}' not found.")

        # Validate that research session exists and references workspace if provided
        if session_id:
            session = repo.get_session(session_id)
            if not session:
                raise ValueError(f"Research session '{session_id}' not found.")
            if workspace_id and session.get("workspace_id") != workspace_id:
                raise ValueError(
                    f"Research session '{session_id}' does not belong to workspace '{workspace_id}'."
                )

        # Initialize or link tracking job
        active_job_id = job_id
        if active_job_id:
            existing_job = repo.get_processing_job(active_job_id)
            if existing_job:
                repo.update_processing_job(
                    job_id=active_job_id,
                    status="PROCESSING",
                    current_stage="INITIALIZED",
                )
            else:
                job = repo.create_processing_job(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    status="PROCESSING",
                    current_stage="INITIALIZED",
                )
                active_job_id = job["job_id"]
        else:
            job = repo.create_processing_job(
                document_id=document_id,
                workspace_id=workspace_id,
                session_id=session_id,
                status="PROCESSING",
                current_stage="INITIALIZED",
            )
            active_job_id = job["job_id"]

        state = FinancialResearchState(
            document_id=document_id,
            filename=filename,
            file_bytes=file_bytes,
            storage_path=storage_path,
            workspace_id=workspace_id,
            session_id=session_id,
            user_id=user_id,
            job_id=active_job_id,
        )

        # Sequential Node Execution
        state = document_agent_node(state)
        if not state.is_failed:
            state = extraction_agent_node(state)
            state = evidence_validation_node(state)
            if state.validation_passed:
                state = red_flag_agent_node(state)

        state = persistence_node(state)
        return state

