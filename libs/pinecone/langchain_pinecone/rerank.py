from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Sequence, Union

from langchain_core.callbacks.base import Callbacks
from langchain_core.documents import BaseDocumentCompressor, Document
from langchain_core.utils import secret_from_env
from pinecone import Pinecone
from pydantic import ConfigDict, Field, SecretStr, model_validator
from typing_extensions import Self

logger = logging.getLogger(__name__)


class PineconeRerank(BaseDocumentCompressor):
    """Document compressor that uses `Pinecone Rerank API`."""

    client: Optional[Any] = None
    """Pinecone client to use for compressing documents."""
    top_n: Optional[int] = 3
    """Number of documents to return."""
    model: Optional[str] = None
    """Model to use for reranking. Mandatory to specify the model name."""
    pinecone_api_key: Optional[SecretStr] = Field(
        default_factory=secret_from_env("PINECONE_API_KEY", default=None)
    )
    """Pinecone API key. Must be specified directly or via environment variable 
    PINECONE_API_KEY."""
    rank_fields: Optional[Sequence[str]] = None
    """Fields to use for reranking when documents are dictionaries."""
    return_documents: bool = True
    """Whether to return the documents in the reranking results."""

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    @model_validator(mode="after")
    def validate_environment(self) -> Self:  # type: ignore[valid-type]
        """Validate that api key and python package exists in environment."""
        if not self.client:
            if isinstance(self.pinecone_api_key, SecretStr):
                pinecone_api_key: Optional[str] = (
                    self.pinecone_api_key.get_secret_value()
                )
            else:
                pinecone_api_key = self.pinecone_api_key

            self.client = Pinecone(api_key=pinecone_api_key)

        elif not isinstance(self.client, object) or not hasattr(
            self.client, "inference"
        ):
            raise ValueError(
                "The 'client' parameter must be an instance of pinecone.Pinecone.\n"
                "You may create the Pinecone object like:\n\n"
                "from pinecone import Pinecone\nclient = Pinecone(api_key=...)"
            )
        return self

    @model_validator(mode="after")
    def validate_model_specified(self) -> Self:  # type: ignore[valid-type]
        """Validate that model is specified."""
        if not self.model:
            raise ValueError(
                "Did not find `model`! Please "
                " pass `model` as a named parameter."
                " Example models include 'bge-reranker-v2-m3'."
            )

        return self

    def _document_to_dict(
        self,
        document: Union[str, Document, dict],
        index: int,
    ) -> dict:
        if isinstance(document, Document):
            doc_id_from_meta = document.metadata.get("id")
            if isinstance(doc_id_from_meta, str) and doc_id_from_meta:
                doc_id = doc_id_from_meta
            else:  # Generate ID if not valid
                doc_id = f"doc_{index}"

            doc_data = {
                "id": doc_id,
                "text": document.page_content,
                **document.metadata,
            }
            return doc_data
        elif isinstance(document, dict):
            current_id = document.get("id")
            if not isinstance(current_id, str) or not current_id:
                document["id"] = f"doc_{index}"  # Generate and set ID if not valid
            return document
        else:
            return {"id": f"doc_{index}", "text": str(document)}

    def rerank(
        self,
        documents: Sequence[Union[str, Document, dict]],
        query: str,
        *,
        rank_fields: Optional[Sequence[str]] = None,
        model: Optional[str] = None,
        top_n: Optional[int] = None,
        truncate: str = "END",
    ) -> List[Dict[str, Any]]:
        """Returns an ordered list of documents ordered by their relevance to the provided query.

        This method reranks documents using Pinecone's reranking API as part of a two-stage
        vector retrieval process to improve result quality. It first converts documents to the
        appropriate format, then sends them along with the query to the reranking model. The
        reranking model scores the results based on their semantic relevance to the query and
        returns a new, more accurate ranking.

        Args:
            query: The query to use for reranking.
            documents: A sequence of documents to rerank. Can be strings, Document objects,
                or dictionaries with an optional 'id' field and text content.
            rank_fields: A sequence of keys to use for reranking when documents are dictionaries.
                Only the first field is used for models that support a single rank field.
            model: The model to use for reranking. Defaults to self.model.
                Supported models include 'bge-reranker-v2-m3', 'pinecone-rerank-v0',
                and 'cohere-rerank-3.5'.
            top_n: The number of results to return. If None returns all results.
                Defaults to self.top_n.
            truncate: How to truncate documents if they exceed token limits. Options: "END",
                "MIDDLE". Defaults to "END".

        Returns:
            A list of dictionaries containing:
                - id: The document ID
                - index: The original index in the input documents sequence
                - score: The relevance score (0-1, with 1 being most relevant)
                - document: The document content (if return_documents=True)

        Examples:
            ```python
            from langchain_pinecone import PineconeRerank
            from langchain_core.documents import Document
            from pinecone import Pinecone

            # Initialize Pinecone client
            pc = Pinecone(api_key="your-api-key")

            # Create the reranker
            reranker = PineconeRerank(
                client=pc,
                model="bge-reranker-v2-m3",
                top_n=2
            )

            # Create sample documents
            documents = [
                Document(page_content="Apple is a popular fruit known for its sweetness."),
                Document(page_content="Apple Inc. has revolutionized the tech industry."),
                Document(page_content="An apple a day keeps the doctor away."),
            ]

            # Rerank documents
            rerank_results = reranker.rerank(
                documents=documents,
                query="Tell me about the tech company Apple",
            )

            # Display results
            for result in rerank_results:
                print(f"Score: {result['score']}, Document: {result['document']}")
            ```

            Using dictionaries with custom fields:
            ```python
            # Create documents as dictionaries with custom fields
            docs = [
                {"id": "doc1", "content": "Apple is a fruit known for its sweetness."},
                {"id": "doc2", "content": "Apple Inc. creates innovative tech products."},
            ]

            # Rerank using a custom field
            results = reranker.rerank(
                documents=docs,
                query="tech companies",
                rank_fields=["content"],
                top_n=1
            )
            ```
        """
        if len(documents) == 0:  # to avoid empty API call
            return []

        docs = [
            self._document_to_dict(document=doc, index=i)
            for i, doc in enumerate(documents)
        ]

        # Handle rank_fields - Pinecone requires exactly one rank field
        effective_rank_fields = rank_fields or self.rank_fields
        if effective_rank_fields and len(effective_rank_fields) > 0:
            # Take only the first rank field if multiple are provided
            rank_field = [effective_rank_fields[0]]
        else:
            # Default to "text" if no rank_fields are provided - this is the default for Pinecone
            rank_field = ["text"]

        top_n = top_n if top_n is not None else self.top_n

        model_name = self.model or model or "bge-reranker-v2-m3"

        parameters = {}
        # Only include truncate parameter for models that support it
        if model_name != "cohere-rerank-3.5":
            parameters["truncate"] = truncate

        try:
            if self.client is None:
                raise ValueError("Pinecone client not found. Please provide a client.")

            rerank_result = self.client.inference.rerank(
                model=model_name,
                query=query,
                documents=docs,
                rank_fields=rank_field,
                top_n=top_n,
                return_documents=self.return_documents,
                parameters=parameters,
            )

            result_dicts = []
            for result_item_data in rerank_result.data:
                result_dict = {
                    "id": result_item_data.document.id,
                    "index": result_item_data.index,
                    "score": result_item_data.score,
                }

                if self.return_documents:
                    result_dict["document"] = result_item_data.document.to_dict()

                result_dicts.append(result_dict)

            return result_dicts

        except Exception as e:
            logger.error(f"Rerank error: {e}")
            return []

    def compress_documents(
        self,
        documents: Sequence[Document],
        query: str,
        callbacks: Optional[Callbacks] = None,
    ) -> Sequence[Document]:
        """
        Compress documents using Pinecone's rerank API.

        Args:
            documents: A sequence of documents to compress.
            query: The query to use for compressing the documents.
            callbacks: Callbacks to run during the compression process.

        Returns:
            A sequence of compressed documents.
        """
        if not documents:
            return []

        compressed = []
        reranked_results = self.rerank(documents, query)

        # If we didn't get any results, return an empty list
        if not reranked_results:
            return []

        for res in reranked_results:
            if res["index"] is not None:
                doc_index = res["index"]
                # Make sure the index is within bounds
                if 0 <= doc_index < len(documents):
                    doc = documents[doc_index]
                    doc_copy = Document(
                        doc.page_content, metadata=deepcopy(doc.metadata)
                    )
                    doc_copy.metadata["relevance_score"] = res["score"]
                    compressed.append(doc_copy)

        return compressed
