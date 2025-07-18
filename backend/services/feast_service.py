from typing import List
from feast import FeatureStore
from models import Product, User
import os
from minio import Minio
import torch
import json
import shutil
from public.models.entity_tower import EntityTower
from public.models.data_util import data_preproccess
from datetime import datetime, timezone
import pandas as pd

class FeastService:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FeastService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.store = FeatureStore('public')
            self._initialized = True
            self.user_encoder = self._load_user_encoder()
            self.user_service = self.store.get_feature_service("user_service")

    def _load_user_encoder(self):
        minio_client = Minio(
            endpoint=os.getenv('MINIO_HOST', "endpoint") + \
                ':' + os.getenv('MINIO_PORT', '9000'),
            access_key=os.getenv('MINIO_ACCESS_KEY', "access-key"),
            secret_key=os.getenv('MINIO_SECRET_KEY', "secret-key"),
            secure=False  # Set to True if using HTTPS
        )
        
        bucket_name = "user-encoder"
        object_name = "user-encoder.pth"
        configuration = 'user-encoder-config.json'
        
        # Download the model file from MinIO
        minio_client.fget_object(bucket_name, object_name, "/tmp/user-encoder.pth")
        minio_client.fget_object(bucket_name, configuration, "/tmp/user-encoder-config.json")
        # Load the model state dict using torch.load
        with open('/tmp/user-encoder-config.json', 'r') as f:
            json_config = json.load(f)
        user_encoder = EntityTower(json_config['users_num_numerical'], json_config['users_num_categorical'])
        user_encoder.load_state_dict(torch.load('/tmp/user-encoder.pth'))
        # Clean up the temporary folder
        
        return user_encoder
    
    def get_all_existing_users(self) -> List[dict]:
        try:
            parquet_path = "data/recommendation_users.parquet"
            users_df = pd.read_parquet(parquet_path)
            user_ids = users_df['user_id'].unique().tolist()

            entity_df = pd.DataFrame({
                'user_id': user_ids,
                'event_timestamp': [datetime.now(timezone.utc)] * len(user_ids)
            })

            user_df = self.store.get_historical_features(
                entity_df=entity_df,
                features=self.user_service
            ).to_df()
            
            print("Fetched all users")

            return user_df.to_dict(orient="records")

        except Exception as e:
            print(f"Failed to fetch users from feature view: {e}")
            return []
    
    def load_items_existing_user(self, user_id: int) -> List[Product]:
        suggested_item_ids = self.store.get_online_features(
            features=self.store.get_feature_service('user_top_k_items'),
            entity_rows=[{'user_id': user_id}]
        )
        
        top_item_ids = suggested_item_ids.to_df().iloc[0]['top_k_item_ids']
        
        return self._item_ids_to_product_list(top_item_ids)


    def load_items_new_user(self, user: User, k: int = 10):
        user_as_df = pd.DataFrame([user.model_dump()])
        
        user_embed = self.user_encoder(**data_preproccess(user_as_df))[0]
        top_k = self.store.retrieve_online_documents(
            query=user_embed.tolist(),
            top_k=k,
            features=['item_embedding:item_id']
        )
        top_item_ids = top_k.to_df().iloc[0]['top_k_item_ids']

        return self._item_ids_to_product_list(top_item_ids)
    
    def _item_ids_to_product_list(self, top_item_ids: pd.Series | List) -> List[Product]:
    
        suggested_item = self.store.get_online_features(
            features=self.store.get_feature_service('item_service'),
            entity_rows=[{'item_id': item_id}  for item_id in top_item_ids]
        ).to_df()
        
        print(suggested_item.columns)
        print(suggested_item)
        # Parse df to list of products
        suggested_item = [Product(
            item_id=row.item_id,
            name=row.name,
            category=row.category,
            subcategory=row.subcategory, 
            price=row.price,
            avg_rating=row.avg_rating,
            num_ratings=row.num_ratings,
            popular=row.popular,
            new_arrival=row.new_arrival,
            on_sale=row.on_sale,
            arrival_date=row.arrival_date,
            description=row.description
        ) for row in suggested_item.itertuples()]
        return suggested_item
# Initilaize the service
feast_service = FeastService()

