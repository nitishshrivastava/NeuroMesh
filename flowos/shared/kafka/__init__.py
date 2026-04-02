"""
shared.kafka — Kafka infrastructure for FlowOS.

This package provides the complete Kafka producer, consumer, topic registry,
payload schemas, and admin client for the FlowOS event bus.

Exports:
    KafkaAdminClient  — Admin client for topic lifecycle management
    FlowOSProducer    — Thread-safe event producer
    FlowOSConsumer    — Handler-based event consumer
    KafkaTopic        — Canonical topic name enum
    ConsumerGroup     — Consumer group identifier enum
    topic_for_event   — Route an EventType to its KafkaTopic
    topics_for_group  — Get topic list for a ConsumerGroup
    build_event       — Factory: construct EventRecord from typed payload
    validate_payload  — Validate a raw payload dict against its schema
    get_producer      — Return the module-level singleton producer
    close_producer    — Flush and close the singleton producer
    make_consumer     — Factory: create a pre-configured consumer
    ErrorStrategy     — Consumer error handling strategy constants
    BaseEventPayload  — Base class for all payload schemas
"""

from shared.kafka.admin import KafkaAdminClient
from shared.kafka.consumer import ErrorStrategy, FlowOSConsumer, make_consumer
from shared.kafka.producer import FlowOSProducer, close_producer, get_producer
from shared.kafka.schemas import BaseEventPayload, build_event, validate_payload
from shared.kafka.topics import (
    ConsumerGroup,
    KafkaTopic,
    TOPIC_METADATA,
    topic_for_event,
    topic_names_for_group,
    topics_for_group,
)

__all__ = [
    # Admin
    "KafkaAdminClient",
    # Producer
    "FlowOSProducer",
    "get_producer",
    "close_producer",
    # Consumer
    "FlowOSConsumer",
    "make_consumer",
    "ErrorStrategy",
    # Topics
    "KafkaTopic",
    "ConsumerGroup",
    "TOPIC_METADATA",
    "topic_for_event",
    "topics_for_group",
    "topic_names_for_group",
    # Schemas
    "BaseEventPayload",
    "build_event",
    "validate_payload",
]
