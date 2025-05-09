# model_training.py
```python
import os
import time
import logging
import numpy as np
import torch
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset, Subset
from sklearn.model_selection import TimeSeriesSplit
import optuna
from ptflops import get_model_complexity_info

from data_preprocessing import load_and_preprocess_data, build_graph, create_sequences
from model_definition import MultiScaleGATv2_LSTM, init_weights, ModelWrapper

# -------------------------------
# Logging and Device Setup
# -------------------------------
logging.basicConfig(level=logging.INFO)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# -------------------------------
# Configuration
# -------------------------------
config = {
        'dynamic_data_path': '/path/to/dynamic.csv',
        'static_data_path': '/path/to/static.csv',
        'grid_data_path': '/path/to/grid.csv',
        'sequence_length': 24,
        'forecast_horizons': [1, 6, 24],
        'train_start_date': '2017-01-01',
        'train_end_date': '2018-12-31',
        'val_start_date': '2019-01-01',
        'val_end_date': '2019-06-30',
        'test_start_date': '2019-07-01',
        'test_end_date': '2019-12-31',
        'output_dir': '/path/to/output'
    }
    
# Ensure output directory exists
os.makedirs(config['output_dir'], exist_ok=True)

# -------------------------------
# Objective for Hyperparameter Optimization
# -------------------------------
def objective(trial):
    # Sample hyperparameters
    gat_out = trial.suggest_int("gat_out_channels", 32, 128, step=32)
    gat_heads = trial.suggest_int("gat_heads", 2, 4)
    lstm_hidden = trial.suggest_int("lstm_hidden_dim", 64, 256, step=64)
    lstm_layers = trial.suggest_int("lstm_layers", 1, 3)
    lr = trial.suggest_float("lr", 1e-4, 1e-3, log=True)
    weight_decay = trial.suggest_float("weight_decay", 1e-6, 1e-3, log=True)

    # Instantiate model
    model = MultiScaleGATv2_LSTM(
        node_feature_dim,
        sequence_feature_dim,
        gat_out,
        gat_heads,
        lstm_hidden,
        lstm_layers,
        edge_dim,
        forecast_horizon,
        reduced_dim=32
    ).to(device)
    model.apply(init_weights)
    model.precompute_embeddings(node_features_tensor, edge_index_tensor, edge_attr_tensor, train_nodes_tensor)

    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=3)
    criterion = torch.nn.MSELoss()

    # Cross-validation
    tscv = TimeSeriesSplit(n_splits=3)
    dataset = TensorDataset(train_sequences, train_targets, train_nodes_tensor)
    indices = np.arange(len(dataset))
    fold_losses = []

    for train_idx, val_idx in tscv.split(indices):
        train_subset = Subset(dataset, train_idx)
        val_subset = Subset(dataset, val_idx)
        train_loader = DataLoader(train_subset, batch_size=32, shuffle=False)
        val_loader = DataLoader(val_subset, batch_size=32, shuffle=False)

        best_val_loss = float('inf')
        patience_counter = 0
        # Train folds
        for epoch in range(50):
            model.train()
            for seq, tgt, nd in train_loader:
                seq, tgt, nd = seq.to(device), tgt.to(device), nd.to(device)
                optimizer.zero_grad()
                out = model(seq, nd)
                loss = criterion(out, tgt)
                loss.backward()
                optimizer.step()
            scheduler.step(best_val_loss)

            # Validation
            model.eval()
            val_loss = 0.0
            with torch.no_grad():
                for seq, tgt, nd in val_loader:
                    seq, tgt, nd = seq.to(device), tgt.to(device), nd.to(device)
                    out = model(seq, nd)
                    val_loss += criterion(out, tgt).item() * seq.size(0)
            val_loss /= len(val_loader.dataset)

            if val_loss < best_val_loss:
                best_val_loss = val_loss
                patience_counter = 0
            else:
                patience_counter += 1
                if patience_counter >= 5:
                    break
        fold_losses.append(best_val_loss)

    return np.mean(fold_losses)

# -------------------------------
# Main Training Routine
# -------------------------------
if __name__ == "__main__":
    # Load and preprocess data
    train_data, val_data, test_data, static_data, grid_df, features_to_scale, target_scaler = \
        load_and_preprocess_data(config)
    # Build graph
    graph_data, node_mapping = build_graph(static_data, train_data, grid_df)
    # Create sequences
    train_sequences, train_targets, train_nodes = create_sequences(
        train_data, features_to_scale, config['sequence_length'], config['forecast_horizons'], node_mapping
    )
    val_sequences, val_targets, val_nodes = create_sequences(
        val_data, features_to_scale, config['sequence_length'], config['forecast_horizons'], node_mapping
    )

    # Prepare tensors and globals for HPO
    node_feature_dim = graph_data.x.size(1)
    sequence_feature_dim = train_sequences.size(2)
    edge_dim = graph_data.edge_attr.size(1)
    forecast_horizon = train_targets.size(1)
    node_features_tensor = graph_data.x.to(device)
    edge_index_tensor = graph_data.edge_index.to(device)
    edge_attr_tensor = graph_data.edge_attr.to(device)
    train_nodes_tensor = train_nodes.to(device)

    # Hyperparameter Optimization
    logging.info("Starting hyperparameter optimization...")
    study = optuna.create_study(
        direction="minimize",
        sampler=optuna.samplers.TPESampler(),
        pruner=optuna.pruners.MedianPruner()
    )
    study.optimize(objective, n_trials=30)
    best_params = study.best_trial.params
    logging.info(f"Best hyperparameters: {best_params}")

    # Instantiate final model with best params
    final_model = MultiScaleGATv2_LSTM(
        node_feature_dim,
        sequence_feature_dim,
        best_params['gat_out_channels'],
        best_params['gat_heads'],
        best_params['lstm_hidden_dim'],
        best_params['lstm_layers'],
        edge_dim,
        forecast_horizon,
        reduced_dim=32
    ).to(device)
    final_model.apply(init_weights)
    final_model.precompute_embeddings(node_features_tensor, edge_index_tensor, edge_attr_tensor, train_nodes_tensor)

    # Optimizer, scheduler, loss
    optimizer = optim.Adam(final_model.parameters(), lr=best_params['lr'], weight_decay=best_params['weight_decay'])
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=5, verbose=True)
    criterion = torch.nn.MSELoss()

    # DataLoaders
    train_loader_final = DataLoader(TensorDataset(train_sequences, train_targets, train_nodes), batch_size=32, shuffle=True)
    val_loader_final   = DataLoader(TensorDataset(val_sequences, val_targets, val_nodes), batch_size=32)

    # Training
    logging.info("Beginning training...")
    start_time = time.time()
    tr_losses, val_losses = train_model(
        final_model, train_loader_final, val_loader_final,
        optimizer, scheduler, criterion, device, num_epochs=200, patience=10
    )
    training_time = time.time() - start_time
    logging.info(f"Total Training Time: {training_time:.2f} seconds")

    # Save model
    model_path = os.path.join(config['output_dir'], 'best_model.pth')
    torch.save(final_model.state_dict(), model_path)
    logging.info(f"Saved trained model to {model_path}")

    # Model complexity analysis
    wrapper = ModelWrapper(final_model, train_nodes_tensor[:1])
    macs, params = get_model_complexity_info(
        wrapper,
        (config['sequence_length'], sequence_feature_dim),
        as_strings=True, print_per_layer_stat=False
    )
    logging.info(f"Model Complexity -> FLOPS: {macs}, Params: {params}")
