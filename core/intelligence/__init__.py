"""Financial Intelligence Platform — public API.

A transparent, explainable layer that turns accounting figures (via the Financial
Metrics Registry and Semantic Reporting Layer) into structured insights,
recommendations, trends/forecasts, a health score, and a unified knowledge
service for a future AI Treasurer. No accounting calculation is duplicated here;
every conclusion is traceable to a registry metric.
"""
from core.intelligence.engine import (
    Insight, Explanation, Severity, Category, Status,
    IntelligenceConfig, IntelligenceModule, IntelligenceEngine,
    intelligence_registry,
)
from core.intelligence.recommendations import (
    Recommendation, recommendations_from_insights,
)
from core.intelligence.health import (
    HealthScore, Indicator, compute_health_score,
)
from core.intelligence import trends
from core.intelligence import knowledge

# import the module library so the registry is populated on package import
from core.intelligence import modules  # noqa: F401

__all__ = [
    "Insight", "Explanation", "Severity", "Category", "Status",
    "IntelligenceConfig", "IntelligenceModule", "IntelligenceEngine",
    "intelligence_registry",
    "Recommendation", "recommendations_from_insights",
    "HealthScore", "Indicator", "compute_health_score",
    "trends", "knowledge",
]
