# FinOps Agent — Server-side execution con Azure Functions

Patrón: las tools del agente ejecutan **server-side** vía Azure Functions con queue triggers, no en cliente Python. Esto permite usar el agente desde:

- **Playground de Foundry** (UI del portal)
- **Power BI / Fabric** (iframe sin servidor propio)
- **Copilot Studio** (citizen developers)
- **Teams** (vía Copilot)
- Cualquier cliente OpenAI-compatible que conozca el agente

## Arquitectura

```
Cliente (Playground / Power BI / Copilot Studio)
    ↓
finops-ai-agent (Foundry, 8 AzureFunctionTool)
    ↓ pone mensaje JSON en queue
Storage Queue (input por tool):
  • finops-resource-costs-input
  • finops-budget-status-input
  • finops-anomalies-input
  • finops-waste-input
  • finops-rightsizing-input
  • finops-tag-compliance-input
  • finops-foundry-models-input
  • finops-dormant-agents-input
    ↓ Queue trigger Azure Functions
finops-func-XXXX (Linux Consumption, Python 3.11)
  function_app.py ejecuta la tool con Managed Identity
    ↓ escribe resultado
Storage Queue: finops-tool-output (compartida)
    ↓
Foundry recoge la respuesta y continúa el chat
```

## Estructura del proyecto

```
finops-functions/
├── function_app.py          # 8 queue triggers + 1 health HTTP
├── requirements.txt
├── host.json
├── local.settings.json      # para `func start` local
├── deploy.ps1               # crea Storage + Functions + queues + permisos
└── register_agent.py        # registra el agente nextgen con las 8 AzureFunctionTool
```

## Deploy completo

### 1. Pre-requisitos

```powershell
# Azure CLI + Azure Functions Core Tools
winget install Microsoft.AzureCLI
winget install Microsoft.Azure.FunctionsCoreTools

az login
```

### 2. Desplegar Function App + Storage + permisos

Desde la carpeta del proyecto:

```powershell
.\deploy.ps1
```

Esto:
1. Crea Storage Account `stfinopsfuncXXXX`
2. Crea las 8 input queues + 1 output queue
3. Crea Function App `finops-func-XXXX` (Linux Consumption, scale-to-zero)
4. Activa Managed Identity y asigna permisos:
   - Function MI → Cost Management Reader, Reader, Storage Queue Data Contributor, Cognitive Services User
   - **Foundry account → Storage Contributor x5** (necesario para que el agente lea/escriba queues)
5. Deploy del código vía ZIP

Salida: archivo `deploy-output.json` con los valores que necesita el siguiente paso.

### 3. Registrar el agente con las tools

```powershell
# Lee storage_queue_uri del deploy-output.json
$config = Get-Content .\deploy-output.json | ConvertFrom-Json
python register_agent.py --storage-uri $config.storage_queue_uri
```

Esto crea una nueva versión de `finops-ai-agent` en el portal Foundry con las 8 `AzureFunctionTool` apuntando a sus queues.

### 4. Probar desde el playground

1. `https://ai.azure.com` → `taagents001-project` → **Operate → Assets → Agents → finops-ai-agent**
2. Tab "Chatear"
3. Prueba:
   - "Resumen de gasto de este mes"
   - "Detecta anomalías de los últimos 30 días"
   - "Top 5 recomendaciones de Azure Advisor"
   - "¿Hay agentes Foundry dormidos?"

Foundry pondrá los mensajes en las queues, las Functions ejecutarán, y verás las respuestas reales con datos del entorno.

## Costes esperados

| Recurso | Coste mensual estimado |
|---|---|
| Storage Account (queues) | < €1 |
| Function App Consumption (scale-to-zero) | €0 si no se usa + ~€0.0002 por ejecución |
| Azure Cost Management API calls | €0 (gratis) |
| Foundry Agent (gpt-4.1 invocations) | depende del uso |

Para 100 invocaciones/día: ~€2-5/mes total infraestructura.

## Embed en Power BI

Con esta arquitectura, el embed en Power BI es trivial — no necesitas tu propio servidor:

**Opción 1 — Copilot Studio publishing**: publica el agente como copiloto, embed nativo en Power BI/Teams.

**Opción 2 — Foundry Agent Playground embed** (si Microsoft lo expone como widget público — requiere verificar).

**Opción 3 — Iframe a una mini-web pública** que llame al agente vía Responses API. Solo el frontend (HTML/JS estático) — el agente y sus tools viven en Azure.

## Troubleshooting

**"No tool output found"**: los permisos de Foundry sobre Storage no están bien. Verifica que el principal de `taagents001` tiene los 5 roles de Storage sobre `stfinopsfuncXXXX`.

**Mensaje en `*-poison` queue**: la Function falló al procesar. Revisa logs en Application Insights:
```powershell
az monitor app-insights query --app finops-func-XXXX --analytics-query "traces | order by timestamp desc | take 50"
```

**Function no se ejecuta**: revisa que `AzureWebJobsFeatureFlags=EnableWorkerIndexing` está en app settings (Python v2 lo requiere).

**Permisos Cost Management lentos en propagar**: pueden tardar 5-10 min tras `az role assignment create`.
