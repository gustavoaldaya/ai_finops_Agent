"""
function_app.py — Azure Functions con HTTP triggers para FinOps Agent
=====================================================================
Cada tool es un HTTP endpoint, no queue trigger.
Foundry usa OpenAPI Tool para llamarlas directamente vía HTTPS.

Sin queues. Sin Capability Hosts. Sin connections de storage.
"""

import json
import logging
from datetime import datetime, timedelta

import azure.functions as func
from azure.identity import ManagedIdentityCredential
from azure.mgmt.costmanagement import CostManagementClient
from azure.mgmt.costmanagement.models import (
    ExportType, GranularityType, QueryAggregation, QueryColumnType,
    QueryDataset, QueryDefinition, QueryGrouping, QueryTimePeriod, TimeframeType,
)
from azure.mgmt.resource import ResourceManagementClient

SUBSCRIPTION_ID = "b66785c5-0678-487e-b2a3-9636082786ea"
RESOURCE_GROUP = "Agentic"
SCOPE = f"/subscriptions/{SUBSCRIPTION_ID}"

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


def _cred():
    return ManagedIdentityCredential()


def _json_response(data: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(data, ensure_ascii=False, default=str),
        mimetype="application/json",
        status_code=status,
    )


# ── Tool 1: get_resource_costs ────────────────────────────────────────────────

@app.route(route="get_resource_costs", methods=["POST", "GET"])
def get_resource_costs(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json() if req.get_body() else {}
        time_range = body.get("time_range") or req.params.get("time_range", "MTD")
        group_by = body.get("group_by") or req.params.get("group_by", "ServiceName")
        top_n = int(body.get("top_n") or req.params.get("top_n", 20))

        cost_client = CostManagementClient(_cred())
        # SDK 4.0.1 solo tiene: MONTH_TO_DATE, THE_LAST_MONTH, WEEK_TO_DATE, CUSTOM
        # Para Last7Days/Last30Days hay que usar CUSTOM con time_period explícito.
        if time_range == "MTD":
            query = QueryDefinition(
                type=ExportType.ACTUAL_COST,
                timeframe=TimeframeType.MONTH_TO_DATE,
                dataset=QueryDataset(
                        aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
                    grouping=[QueryGrouping(type=QueryColumnType.DIMENSION, name=group_by)],
                ),
            )
        elif time_range == "LastMonth":
            query = QueryDefinition(
                type=ExportType.ACTUAL_COST,
                timeframe=TimeframeType.THE_LAST_MONTH,
                dataset=QueryDataset(
                        aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
                    grouping=[QueryGrouping(type=QueryColumnType.DIMENSION, name=group_by)],
                ),
            )
        else:
            days = 7 if time_range == "Last7Days" else 30
            end = datetime.utcnow()
            start = end - timedelta(days=days)
            query = QueryDefinition(
                type=ExportType.ACTUAL_COST,
                timeframe=TimeframeType.CUSTOM,
                time_period=QueryTimePeriod(from_property=start, to=end),
                dataset=QueryDataset(
                        aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
                    grouping=[QueryGrouping(type=QueryColumnType.DIMENSION, name=group_by)],
                ),
            )
        result = cost_client.query.usage(scope=SCOPE, parameters=query)
        rows = sorted(
            [{"name": r[1], "cost_eur": round(float(r[0]), 2)} for r in result.rows],
            key=lambda x: x["cost_eur"], reverse=True,
        )[:top_n]
        return _json_response({
            "subscription": SUBSCRIPTION_ID,
            "time_range": time_range,
            "group_by": group_by,
            "total_eur": round(sum(r["cost_eur"] for r in rows), 2),
            "top_items": rows,
        })
    except Exception as e:
        logging.exception("get_resource_costs failed")
        return _json_response({"error": str(e)}, 500)


# ── Tool 2: get_budget_status ─────────────────────────────────────────────────

@app.route(route="get_budget_status", methods=["POST", "GET"])
def get_budget_status(req: func.HttpRequest) -> func.HttpResponse:
    try:
        from azure.mgmt.consumption import ConsumptionManagementClient
        consumption = ConsumptionManagementClient(_cred(), SUBSCRIPTION_ID)
        budgets = list(consumption.budgets.list(scope=SCOPE))
        budget_list = []
        alerts = []
        for b in budgets:
            spend = b.current_spend.amount if b.current_spend else 0
            limit = b.amount
            pct = round((spend / limit) * 100, 1) if limit > 0 else 0
            status = "OK"
            if pct >= 100:
                status = "EXCEEDED"
                alerts.append(f"'{b.name}' EXCEDIDO: {pct}%")
            elif pct >= 80:
                status = "WARNING"
                alerts.append(f"'{b.name}' WARNING: {pct}%")
            budget_list.append({
                "name": b.name, "limit_eur": limit, "spent_eur": round(spend, 2),
                "pct_consumed": pct, "status": status,
            })
        return _json_response({"budgets": budget_list, "alerts": alerts})
    except Exception as e:
        logging.exception("get_budget_status failed")
        return _json_response({"error": str(e)}, 500)


# ── Tool 3: analyze_cost_anomalies ────────────────────────────────────────────

@app.route(route="analyze_cost_anomalies", methods=["POST", "GET"])
def analyze_cost_anomalies(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json() if req.get_body() else {}
        lookback = int(body.get("lookback_days") or req.params.get("lookback_days", 30))
        threshold = float(body.get("spike_threshold_pct") or req.params.get("spike_threshold_pct", 25.0))

        cost_client = CostManagementClient(_cred())
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=lookback)
        query = QueryDefinition(
            type=ExportType.ACTUAL_COST,
            timeframe=TimeframeType.CUSTOM,
            time_period=QueryTimePeriod(from_property=start_date, to=end_date),
            dataset=QueryDataset(
                granularity=GranularityType.DAILY,
                aggregation={"totalCost": QueryAggregation(name="Cost", function="Sum")},
                grouping=[QueryGrouping(type=QueryColumnType.DIMENSION, name="ServiceName")],
            ),
        )
        result = cost_client.query.usage(scope=SCOPE, parameters=query)
        service_daily = {}
        for row in result.rows:
            cost, service = float(row[0]), row[2]
            service_daily.setdefault(service, []).append({"date": str(row[1]), "cost": cost})

        anomalies = []
        for service, daily in service_daily.items():
            if len(daily) < 7:
                continue
            daily.sort(key=lambda x: x["date"])
            historical = [d["cost"] for d in daily[:-1]]
            mean = sum(historical) / len(historical)
            last = daily[-1]["cost"]
            if mean > 0:
                dev_pct = ((last - mean) / mean) * 100
                if abs(dev_pct) >= threshold:
                    anomalies.append({
                        "service": service,
                        "last_cost_eur": round(last, 2),
                        "mean_cost_eur": round(mean, 2),
                        "deviation_pct": round(dev_pct, 1),
                        "type": "SPIKE" if dev_pct > 0 else "DROP",
                    })
        anomalies.sort(key=lambda x: abs(x["deviation_pct"]), reverse=True)
        return _json_response({"anomalies_found": len(anomalies), "anomalies": anomalies[:10]})
    except Exception as e:
        logging.exception("analyze_cost_anomalies failed")
        return _json_response({"error": str(e)}, 500)


# ── Tool 4: get_waste_report ──────────────────────────────────────────────────

@app.route(route="get_waste_report", methods=["POST", "GET"])
def get_waste_report(req: func.HttpRequest) -> func.HttpResponse:
    try:
        resource_client = ResourceManagementClient(_cred(), SUBSCRIPTION_ID)
        waste = []
        savings = 0.0
        for vm in resource_client.resources.list(filter="resourceType eq 'Microsoft.Compute/virtualMachines'"):
            props = vm.properties or {}
            ps = props.get("extended", {}).get("instanceView", {}).get("powerState", {}).get("code", "")
            if ps == "PowerState/deallocated":
                waste.append({"name": vm.name, "type": "VM_DEALLOCATED_WITH_DISK", "rg": vm.resource_group, "monthly_eur": 42.0})
                savings += 42.0
        for ip in resource_client.resources.list(filter="resourceType eq 'Microsoft.Network/publicIPAddresses'"):
            if not (ip.properties or {}).get("ipConfiguration"):
                waste.append({"name": ip.name, "type": "UNATTACHED_PUBLIC_IP", "rg": ip.resource_group, "monthly_eur": 3.65})
                savings += 3.65
        return _json_response({
            "waste_items_found": len(waste),
            "estimated_monthly_savings_eur": round(savings, 2),
            "waste_items": waste[:20],
        })
    except Exception as e:
        logging.exception("get_waste_report failed")
        return _json_response({"error": str(e)}, 500)


# ── Tool 5: get_rightsizing_recommendations ───────────────────────────────────

@app.route(route="get_rightsizing_recommendations", methods=["POST", "GET"])
def get_rightsizing_recommendations(req: func.HttpRequest) -> func.HttpResponse:
    try:
        from azure.mgmt.advisor import AdvisorManagementClient
        advisor = AdvisorManagementClient(_cred(), SUBSCRIPTION_ID)
        recs = []
        total = 0.0
        for rec in list(advisor.recommendations.list(filter="Category eq 'Cost'"))[:20]:
            props = rec.additional_properties or {}
            s = float(props.get("savingsAmount", 0) or 0)
            total += s
            recs.append({
                "impact": str(rec.impact),
                "problem": rec.short_description.problem if rec.short_description else "",
                "solution": rec.short_description.solution if rec.short_description else "",
                "monthly_savings_eur": round(s, 2),
            })
        recs.sort(key=lambda x: x["monthly_savings_eur"], reverse=True)
        return _json_response({
            "recommendations_count": len(recs),
            "total_savings_eur": round(total, 2),
            "recommendations": recs,
        })
    except Exception as e:
        logging.exception("get_rightsizing_recommendations failed")
        return _json_response({"error": str(e)}, 500)


# ── Tool 6: get_tag_compliance ────────────────────────────────────────────────

@app.route(route="get_tag_compliance", methods=["POST", "GET"])
def get_tag_compliance(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json() if req.get_body() else {}
        required_tags = body.get("required_tags") or ["cost-center", "owner", "environment", "project"]
        resource_client = ResourceManagementClient(_cred(), SUBSCRIPTION_ID)
        resources = list(resource_client.resources.list(filter=f"resourceGroup eq '{RESOURCE_GROUP}'"))
        total = len(resources)
        coverage = {t: 0 for t in required_tags}
        offenders = []
        for r in resources:
            tags = r.tags or {}
            missing = [t for t in required_tags if t not in tags]
            for t in required_tags:
                if t in tags:
                    coverage[t] += 1
            if missing:
                offenders.append({"name": r.name, "type": r.type, "missing": missing})
        pct = {t: round((c / total) * 100, 1) if total > 0 else 0 for t, c in coverage.items()}
        overall = round(sum(pct.values()) / len(pct), 1) if pct else 0
        status = "OK" if overall >= 100 else ("WARNING" if overall >= 80 else "CRITICAL")
        return _json_response({
            "resource_group": RESOURCE_GROUP,
            "total_resources": total,
            "overall_coverage_pct": overall,
            "status": status,
            "coverage_by_tag": pct,
            "offenders_count": len(offenders),
            "top_offenders": offenders[:15],
        })
    except Exception as e:
        logging.exception("get_tag_compliance failed")
        return _json_response({"error": str(e)}, 500)


# ── Tool 7: get_foundry_model_costs ───────────────────────────────────────────

@app.route(route="get_foundry_model_costs", methods=["POST", "GET"])
def get_foundry_model_costs(req: func.HttpRequest) -> func.HttpResponse:
    return _json_response({
        "deployed_models": ["gpt-4.1", "gpt-4.1-mini", "text-embedding-3-large"],
        "pricing_eur_per_1m_tokens": [
            {"model": "gpt-4.1", "input": 2.00, "output": 8.00, "cached_input": 0.50},
            {"model": "gpt-4.1-mini", "input": 0.40, "output": 1.60, "cached_input": 0.10},
            {"model": "text-embedding-3-large", "input": 0.13, "output": 0.00, "cached_input": 0.00},
        ],
        "recommendations": [
            {"pattern": "LLM-03_CASCADE", "description": "Routing gpt-4.1 -> gpt-4.1-mini para queries simples", "savings_pct": 60},
            {"pattern": "PROMPT_CACHING", "description": "Reestructurar system prompts para >40% cache hit", "savings_pct": 20},
            {"pattern": "EMBEDDING_BATCH", "description": "text-embedding-3-large en Batch API (50% descuento)", "savings_pct": 50},
        ],
    })


# ── Tool 8: list_dormant_agents ───────────────────────────────────────────────

@app.route(route="list_dormant_agents", methods=["POST", "GET"])
def list_dormant_agents(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json() if req.get_body() else {}
        threshold_days = int(body.get("dormant_threshold_days") or req.params.get("dormant_threshold_days", 14))

        from azure.ai.projects import AIProjectClient
        PROJECT_ENDPOINT = "https://taagents001.services.ai.azure.com/api/projects/taagents001-project"
        client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_cred())
        agents = list(client.agents.list())
        cutoff = datetime.utcnow() - timedelta(days=threshold_days)
        dormant = []
        active = []
        for a in agents:
            created = getattr(a, "created_at", None) or getattr(a, "creation_time", None)
            if created and created.replace(tzinfo=None) < cutoff:
                dormant.append({"name": a.name, "created_at": str(created)})
            else:
                active.append({"name": a.name})
        total = len(agents)
        pct = round((len(dormant) / total) * 100, 1) if total > 0 else 0
        return _json_response({
            "total_agents": total,
            "active_agents": len(active),
            "dormant_agents": len(dormant),
            "dormant_pct": pct,
            "status": "OK" if pct <= 5 else ("WARNING" if pct <= 15 else "ALERT"),
            "dormant_list": dormant,
        })
    except Exception as e:
        logging.exception("list_dormant_agents failed")
        return _json_response({"error": str(e)}, 500)


# ── Health ────────────────────────────────────────────────────────────────────

@app.route(route="health", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return _json_response({"status": "ok", "tools": 8})