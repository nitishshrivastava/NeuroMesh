"""
shared/kafka/admin.py — FlowOS Kafka Admin Client

Reads topic definitions from ``infra/kafka/topics.json`` and creates all
topics on the Kafka cluster.  Idempotent: topics that already exist are
skipped without error.

Usage (programmatic):
    from shared.kafka.admin import KafkaAdminClient
    client = KafkaAdminClient()
    client.create_topics()

Usage (CLI / Makefile):
    python -m shared.kafka.admin
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from confluent_kafka.admin import (
    AdminClient,
    ConfigResource,
    NewTopic,
)
from confluent_kafka import KafkaException

from shared.config import settings

logger = logging.getLogger(__name__)

# Path to the topics definition file, relative to the project root.
_TOPICS_JSON_PATH = Path(__file__).parent.parent.parent / "infra" / "kafka" / "topics.json"


class KafkaAdminClient:
    """
    Manages Kafka topic lifecycle for FlowOS.

    Reads topic definitions from ``infra/kafka/topics.json`` and creates
    topics that do not yet exist.  Existing topics are left unchanged.
    """

    def __init__(
        self,
        topics_json_path: Path | None = None,
        max_retries: int = 10,
        retry_delay_seconds: float = 3.0,
    ) -> None:
        """
        Initialise the admin client.

        Args:
            topics_json_path: Override path to the topics JSON file.
                              Defaults to ``infra/kafka/topics.json``.
            max_retries: Number of times to retry connecting to Kafka before
                         giving up.
            retry_delay_seconds: Seconds to wait between connection retries.
        """
        self._topics_json_path = topics_json_path or _TOPICS_JSON_PATH
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._admin: AdminClient | None = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def create_topics(self) -> None:
        """
        Read topic definitions from JSON and create all missing topics.

        This method is idempotent — topics that already exist are silently
        skipped.  Raises ``RuntimeError`` if the Kafka cluster is unreachable
        after all retries are exhausted.
        """
        topic_definitions = self._load_topic_definitions()
        admin = self._get_admin_client()

        new_topics = self._build_new_topics(topic_definitions)
        if not new_topics:
            logger.info("No topics defined in %s — nothing to create.", self._topics_json_path)
            return

        logger.info(
            "Attempting to create %d Kafka topic(s): %s",
            len(new_topics),
            [t.topic for t in new_topics],
        )

        # create_topics() returns a dict of {topic_name: Future}
        futures: dict[str, Any] = admin.create_topics(
            new_topics,
            validate_only=False,
        )

        created: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for topic_name, future in futures.items():
            try:
                future.result()  # Blocks until the operation completes
                created.append(topic_name)
                logger.info("✔ Created topic: %s", topic_name)
            except KafkaException as exc:
                # Error code 36 = TOPIC_ALREADY_EXISTS — this is expected and fine.
                if exc.args[0].code() == 36:  # TOPIC_ALREADY_EXISTS
                    skipped.append(topic_name)
                    logger.debug("Topic already exists (skipped): %s", topic_name)
                else:
                    failed.append(topic_name)
                    logger.error(
                        "Failed to create topic %s: %s",
                        topic_name,
                        exc,
                        exc_info=True,
                    )

        # Summary
        logger.info(
            "Topic creation complete — created: %d, already existed: %d, failed: %d",
            len(created),
            len(skipped),
            len(failed),
        )

        if failed:
            raise RuntimeError(
                f"Failed to create the following Kafka topics: {failed}. "
                "Check the logs above for details."
            )

    def delete_topics(self, topic_names: list[str]) -> None:
        """
        Delete the specified Kafka topics.

        Args:
            topic_names: List of topic names to delete.

        Warning:
            This is a destructive operation.  All messages in the topics will
            be permanently lost.
        """
        admin = self._get_admin_client()
        logger.warning("Deleting Kafka topics: %s", topic_names)

        futures: dict[str, Any] = admin.delete_topics(topic_names)

        for topic_name, future in futures.items():
            try:
                future.result()
                logger.info("✔ Deleted topic: %s", topic_name)
            except KafkaException as exc:
                logger.error("Failed to delete topic %s: %s", topic_name, exc)

    def list_topics(self) -> list[str]:
        """
        Return a list of all topic names on the Kafka cluster.

        Returns:
            Sorted list of topic name strings.
        """
        admin = self._get_admin_client()
        metadata = admin.list_topics(timeout=10)
        return sorted(metadata.topics.keys())

    def topic_exists(self, topic_name: str) -> bool:
        """
        Check whether a topic exists on the Kafka cluster.

        Args:
            topic_name: The topic name to check.

        Returns:
            True if the topic exists, False otherwise.
        """
        return topic_name in self.list_topics()

    def describe_topics(self, topic_names: list[str] | None = None) -> dict[str, Any]:
        """
        Return metadata for the specified topics (or all topics if None).

        Args:
            topic_names: List of topic names to describe.  If None, all
                         topics are described.

        Returns:
            Dict mapping topic name to its TopicMetadata object.
        """
        admin = self._get_admin_client()
        metadata = admin.list_topics(timeout=10)

        if topic_names is None:
            return dict(metadata.topics)

        return {
            name: metadata.topics[name]
            for name in topic_names
            if name in metadata.topics
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_admin_client(self) -> AdminClient:
        """
        Return a connected AdminClient, creating one if necessary.

        Retries the connection up to ``max_retries`` times with exponential
        back-off to handle the case where Kafka is still starting up.
        """
        if self._admin is not None:
            return self._admin

        admin_config = settings.kafka.as_admin_config()
        # Reduce the metadata request timeout so we fail fast on each attempt
        admin_config["socket.timeout.ms"] = 5000
        admin_config["metadata.request.timeout.ms"] = 5000

        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                client = AdminClient(admin_config)
                # Probe the cluster to verify connectivity
                client.list_topics(timeout=5)
                logger.info(
                    "Connected to Kafka at %s (attempt %d/%d)",
                    settings.kafka.bootstrap_servers,
                    attempt,
                    self._max_retries,
                )
                self._admin = client
                return self._admin
            except KafkaException as exc:
                last_exc = exc
                logger.warning(
                    "Kafka not yet reachable (attempt %d/%d): %s — retrying in %.1fs…",
                    attempt,
                    self._max_retries,
                    exc,
                    self._retry_delay_seconds,
                )
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay_seconds)

        raise RuntimeError(
            f"Could not connect to Kafka at {settings.kafka.bootstrap_servers} "
            f"after {self._max_retries} attempts. Last error: {last_exc}"
        ) from last_exc

    def _load_topic_definitions(self) -> list[dict[str, Any]]:
        """
        Load and validate topic definitions from the JSON file.

        Returns:
            List of topic definition dicts from the ``topics`` array.

        Raises:
            FileNotFoundError: If the topics JSON file does not exist.
            ValueError: If the JSON structure is invalid.
        """
        if not self._topics_json_path.exists():
            raise FileNotFoundError(
                f"Topics definition file not found: {self._topics_json_path}. "
                "Ensure infra/kafka/topics.json exists."
            )

        with self._topics_json_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        if "topics" not in data or not isinstance(data["topics"], list):
            raise ValueError(
                f"Invalid topics.json format: expected a top-level 'topics' array. "
                f"Got keys: {list(data.keys())}"
            )

        topics = data["topics"]
        logger.debug("Loaded %d topic definition(s) from %s", len(topics), self._topics_json_path)
        return topics

    def _build_new_topics(self, topic_definitions: list[dict[str, Any]]) -> list[NewTopic]:
        """
        Convert topic definition dicts into confluent-kafka ``NewTopic`` objects.

        Args:
            topic_definitions: List of topic definition dicts from topics.json.

        Returns:
            List of ``NewTopic`` objects ready to pass to ``create_topics()``.
        """
        new_topics: list[NewTopic] = []

        for defn in topic_definitions:
            name = defn.get("name")
            if not name:
                logger.warning("Skipping topic definition with missing 'name': %s", defn)
                continue

            partitions: int = defn.get("partitions", 12)
            replication_factor: int = defn.get("replication_factor", 1)
            config: dict[str, str] = defn.get("config", {})

            # Ensure all config values are strings (Kafka requirement)
            str_config = {k: str(v) for k, v in config.items()}

            new_topics.append(
                NewTopic(
                    topic=name,
                    num_partitions=partitions,
                    replication_factor=replication_factor,
                    config=str_config,
                )
            )

        return new_topics


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """
    CLI entry point for creating Kafka topics.

    Called by ``make kafka-topics`` and ``python -m shared.kafka.admin``.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    logger.info("FlowOS Kafka Admin — creating topics…")
    logger.info("Kafka bootstrap servers: %s", settings.kafka.bootstrap_servers)

    client = KafkaAdminClient()
    try:
        client.create_topics()
        logger.info("All Kafka topics are ready.")
        sys.exit(0)
    except RuntimeError as exc:
        logger.error("Topic creation failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
