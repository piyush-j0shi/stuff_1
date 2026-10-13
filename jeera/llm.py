from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from jeera import config

model = ChatOpenAI(model=config.llm_model, api_key=config.llm_api_key, base_url=config.openrouter_url, temperature=0.2)
# openrouter takes plain text, so skip the tokenizer based length check
embedder = OpenAIEmbeddings(model=config.embedding_model, api_key=config.embedding_api_key, base_url=config.openrouter_url,
                            check_embedding_ctx_length=False)
