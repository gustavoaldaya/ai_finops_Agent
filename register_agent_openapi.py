"""
register_agent_openapi.py v5 — usa project connection auth
============================================================
La function key se referencia desde una connection del proyecto Foundry,
no se embebe en el spec.
"""

import argparse

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    OpenApiTool,
    OpenApiFunctionDefinition,
    OpenApiProjectConnectionAuthDetails,
    OpenApiProjectConnectionSecurityScheme,
    PromptAgentDefinition,
)

PROJECT_ENDPOINT = "https://taagents001.services.ai.azure.com/api/projects/taagents001-project"
AGENT_NAME = "finops-ai-agent"
MODEL = "gpt-4.1"

# Connection id completo de finops_func_key
CONNECTION_ID = "/subscriptions/b66785c5-0678-487e-b2a3-9636082786ea/resourceGroups/Agentic/providers/Microsoft.CognitiveServices/accounts/taagents001/projects/taagents001-project/connections/finops_func_key"


FINOPS_SYSTEM_PROMPT = """Eres un agente FinOps de Azure que SIEMPRE usa sus tools para responder.

## ENTORNO
- Suscripcion: b66785c5-0678-487e-b2a3-9636082786ea
- Resource Group: Agentic (France Central)
- Proyecto Foundry: taagents001 / taagents001-project
- Modelos: gpt-4.1, gpt-4.1-mini, text-embedding-3-large

## REGLAS
- SIEMPRE llama a una tool antes de responder con datos numericos.
- NUNCA digas "no tengo acceso a Azure Cost Management".
- NUNCA sugieras comandos az al usuario.
- Si una tool falla, dilo explicitamente.

## FORMATO
1. Hallazgo con datos reales
2. Impacto en EUR y % del gasto
3. Accion concreta con prioridad CRITICA/ALTA/MEDIA/BAJA
4. Esfuerzo en horas
"""


def build_openapi_spec(base_url: str) -> dict:
    """OpenAPI spec sin auth embebido — Foundry lo añade via connection."""
    return {
        "openapi": "3.0.1",
        "info": {
            "title": "FinOps Tools API",
            "version": "1.0.0",
            "description": "Tools FinOps Azure.",
        },
        "servers": [{"url": base_url}],
        "security": [{"functionKey": []}],
        "components": {
            "securitySchemes": {
                "functionKey": {"type": "apiKey", "in": "query", "name": "code"}
            }
        },
        "paths": {
            "/api/get_resource_costs": {
                "post": {
                    "operationId": "get_resource_costs",
                    "summary": "Desglose REAL de costes Azure agrupado por servicio, RG o tipo. OBLIGATORIO para cualquier pregunta sobre cuanto se gasta.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "time_range": {"type": "string", "enum": ["MTD", "LastMonth", "Last7Days", "Last30Days"]},
                                        "group_by": {"type": "string", "enum": ["ServiceName", "ResourceGroupName", "ResourceType"]},
                                        "top_n": {"type": "integer"},
                                    },
                                    "required": ["time_range", "group_by", "top_n"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/api/get_budget_status": {
                "post": {
                    "operationId": "get_budget_status",
                    "summary": "Estado real de los budgets, % consumido, alertas si >80% o 100%.",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/api/analyze_cost_anomalies": {
                "post": {
                    "operationId": "analyze_cost_anomalies",
                    "summary": "Anomalias de coste comparando gasto diario contra media movil.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "lookback_days": {"type": "integer"},
                                        "spike_threshold_pct": {"type": "number"},
                                    },
                                    "required": ["lookback_days", "spike_threshold_pct"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/api/get_waste_report": {
                "post": {
                    "operationId": "get_waste_report",
                    "summary": "Recursos desperdiciados: VMs deallocated, IPs sin asignar.",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/api/get_rightsizing_recommendations": {
                "post": {
                    "operationId": "get_rightsizing_recommendations",
                    "summary": "Recomendaciones de Azure Advisor categoria Cost.",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/api/get_tag_compliance": {
                "post": {
                    "operationId": "get_tag_compliance",
                    "summary": "Cobertura de tags FinOps en RG Agentic. Alerta si <100%.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "required_tags": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": ["required_tags"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/api/get_foundry_model_costs": {
                "post": {
                    "operationId": "get_foundry_model_costs",
                    "summary": "Modelos taagents001 pricing y recomendaciones de optimizacion.",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            "/api/list_dormant_agents": {
                "post": {
                    "operationId": "list_dormant_agents",
                    "summary": "Agentes Foundry sin actividad reciente. Alerta si >15% del fleet.",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "dormant_threshold_days": {"type": "integer"},
                                    },
                                    "required": ["dormant_threshold_days"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--function-app", required=True)
    args = parser.parse_args()

    base_url = f"https://{args.function_app}.azurewebsites.net"
    print(f"Function URL:  {base_url}")
    print(f"Connection:    finops_func_key (project connection)")
    print(f"Agent:         {AGENT_NAME}")

    spec = build_openapi_spec(base_url)

    # Auth via connection — Foundry inyecta la key ?code= en la URL
    auth = OpenApiProjectConnectionAuthDetails(
        security_scheme=OpenApiProjectConnectionSecurityScheme(
            project_connection_id=CONNECTION_ID,
        )
    )

    tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name="finops_tools",
            description="8 tools FinOps que consultan Azure Cost Management, Resource Management, Advisor y Foundry en tiempo real.",
            spec=spec,
            auth=auth,
        )
    )

    project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential())

    agent_version = project.agents.create_version(
        agent_name=AGENT_NAME,
        definition=PromptAgentDefinition(
            model=MODEL,
            instructions=FINOPS_SYSTEM_PROMPT,
            tools=[tool],
            temperature=0.0,
        ),
        description="FinOps AI Agent v5 — OpenAPI Tool + project connection auth.",
        metadata={
            "iteration": "v5-openapi-connection",
        },
    )

    print()
    print("Agente registrado")
    print(f"  Name:    {agent_version.name}")
    print(f"  Version: {agent_version.version}")


if __name__ == "__main__":
    main()