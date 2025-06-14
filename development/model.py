'''

author: Shashwat Ghevde

OPNN

'''
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import grad
from torch.utils.data import DataLoader, TensorDataset
import torch.optim as optim
import numpy as np
from scipy.stats import norm

class OPNN(nn.Module):

    '''

    Main Neural Net Architecture subclassing nn.Module

    Notes: I used a deeper, wider layer. Modeling off Black Scholes is complex so it provides more capacity to learn

    '''
    def __init__(self, input_dim=3, hidden_dim=64, num_hidden_layers=4):
        
        super(OPNN, self).__init__()
        
        layers = []
        
        
        layers.extend([
            nn.Linear(input_dim, hidden_dim),
            nn.SiLU()  # found that SiLU works better than Tanh() https://arxiv.org/pdf/1811.03378
        ])
        
        
        # hidden layers
        for _ in range(num_hidden_layers - 1):
            layers.extend([
                nn.Linear(hidden_dim, hidden_dim),
                nn.SiLU()
            ])
        
        # output layers allows full range for values
        layers.append(nn.Linear(hidden_dim, 1))
        
        self.model = nn.Sequential(*layers)

        self._init_weights()
    
    # https://proceedings.mlr.press/v9/glorot10a/glorot10a.pdf

    # explicity set the weights to bias to zeros. i found that the gradient had started MASSIVELY growing so this regulates it.
    def _init_weights(self):

        for m in self.modules():

            if isinstance(m, nn.Linear):

                nn.init.xavier_normal_(m.weight)
                nn.init.zeros_(m.bias)
    
    def forward(self, x):
        
        output = self.model(x)
        
        return F.softplus(output) # defined in paper, needed to be differentiable throughout


def bs_pde_loss(model, inputs, r=0.05, sigma=0.2):
    """
    PDE loss
    """
    
    inputs_grad = inputs.detach().clone().requires_grad_(True)
    
    S_ratio = inputs_grad[:, 0:1]  # S/K 
    K_norm = inputs_grad[:, 1:2]   # K/100
    T = inputs_grad[:, 2:3]        # T
    
    K = K_norm * 100.0
    S = S_ratio * K
    
    
    C = model(inputs_grad)
    
    
    dC_dS = grad(C, S_ratio, grad_outputs=torch.ones_like(C), create_graph=True, allow_unused=True)[0]
    
    # ADDED SAFETY CHECK FOR GRADIENTS, I kept running into errors with NoneType gradients
    if dC_dS is None:
        return torch.tensor(0.0, requires_grad=True)
    
    dC_dS = dC_dS / K
    
    dC_dT = grad(C, T, grad_outputs=torch.ones_like(C), create_graph=True, allow_unused=True)[0]

    if dC_dT is None:
        return torch.tensor(0.0, requires_grad=True)
    
    d2C_dS2 = grad(dC_dS, S_ratio, grad_outputs=torch.ones_like(dC_dS), create_graph=True, allow_unused=True)[0]
    
    if d2C_dS2 is None:
    
        return torch.tensor(0.0, requires_grad=True)
    
    d2C_dS2 = d2C_dS2 / (K**2)
    
    # Black-Scholes PDE: dC/dt + 0.5*σ²*S²*d²C/dS² + r*S*dC/dS - r*C = 0
    pde_residual = dC_dT + 0.5 * sigma**2 * S**2 * d2C_dS2 + r * S * dC_dS - r * C
    
    return torch.mean(pde_residual**2)

def boundary_loss(model, inputs, r=0.05):
    """
    
    Enforces the boundary conditions for call options
    
    """

    S_ratio = inputs[:, 0:1]  # S/K ratio
    K_norm = inputs[:, 1:2]   # K/100
    T = inputs[:, 2:3]        # T
    
    K = K_norm * 100.0
    S = S_ratio * K
    
    loss = 0.0
    
    # At expiry (T≈0): C = max(S-K, 0)

    # THEORY: At expiry of call option, its value is just its INSTRINSIC value (max(s - k, 0))
    t_zero_mask = T < 0.05  # Near expiry

    if t_zero_mask.sum() > 0:

        t_zero_inputs = inputs[t_zero_mask.squeeze()]
        
        if len(t_zero_inputs) > 0:

            S_exp = t_zero_inputs[:, 0:1] * (t_zero_inputs[:, 1:2] * 100.0)  # S = (S/K) * K
            K_exp = t_zero_inputs[:, 1:2] * 100.0  # K
            C_pred = model(t_zero_inputs)
            C_true = torch.maximum(S_exp - K_exp, torch.zeros_like(S_exp))
            loss += F.mse_loss(C_pred, C_true)
    
    # Deep in-the-money: C ≈ S - K*exp(-r*T)

    # Theory: If a call option is DEEP, then the value approaches the value of holding the stock - present value of the stock
    # JUSTIFICATION: Since the probability extending an option is low, then its basically like owning the stock

    deep_itm_mask = S_ratio > 1.5  # S/K > 1.5
    
    if deep_itm_mask.sum() > 0:
    
        deep_itm_inputs = inputs[deep_itm_mask.squeeze()]
    
        if len(deep_itm_inputs) > 0:
            S_itm = deep_itm_inputs[:, 0:1] * (deep_itm_inputs[:, 1:2] * 100.0)
            K_itm = deep_itm_inputs[:, 1:2] * 100.0
            T_itm = deep_itm_inputs[:, 2:3]
            C_pred = model(deep_itm_inputs)
            C_approx = S_itm - K_itm * torch.exp(-r * T_itm)
            loss += 0.1 * F.mse_loss(C_pred, C_approx)
    
    return loss

def supervised_loss(model, inputs, targets):

    '''

    MSE Loss function

    '''

    predicted_C = model(inputs)
    return F.mse_loss(predicted_C, targets)

def total_loss(model, inputs, targets, lambda_sup=1.0, lambda_pde=0.1, lambda_boundary=0.5, r=0.05, sigma=0.2):
    """
    
    Total loss including accurate weighting and proper boundary loss
    
    Big edit with the change in PDE because its residuals are march larger in magnitude than the supervised MSE.
    """
    sup_loss = supervised_loss(model, inputs, targets)
    pde_loss = bs_pde_loss(model, inputs, r=r, sigma=sigma)
    boundary_loss_val = boundary_loss(model, inputs, r=r)
    
    return lambda_sup * sup_loss + lambda_pde * pde_loss + lambda_boundary * boundary_loss_val


def black_scholes_call_price(S, K, T, r, sigma):
    
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)

def generate_black_scholes_dataset(n_samples=10000, r=0.05, sigma=0.2, seed=42):
    np.random.seed(seed)
    
    
    S = np.random.uniform(0.5, 2.0, size=n_samples)  # S/K ratio
    K = np.random.uniform(80, 120, size=n_samples)   # Strike around 100
    T = np.random.uniform(0.02, 1.0, size=n_samples)  # TTM
    
    C = black_scholes_call_price(S * K, K, T, r, sigma)
    
    raw_inputs = np.stack([S, K/100.0, T], axis=1)
    
    y_tensor = torch.tensor(C.reshape(-1, 1), dtype=torch.float32)
    X_tensor = torch.tensor(raw_inputs, dtype=torch.float32)
    
    return X_tensor, y_tensor


def train_model():
    
    X_train, y_train = generate_black_scholes_dataset(n_samples=20000, r=0.05, sigma=0.2, seed=42)
    
    
    model = OPNN(input_dim=3, hidden_dim=64, num_hidden_layers=4)
    
    
    optimizer = optim.AdamW(model.parameters(), lr=0.001, weight_decay=1e-5)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=20, factor=0.5)
    
    
    batch_size = 256
    train_dataset = TensorDataset(X_train, y_train)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    
    
    num_epochs = 500
    r = 0.05
    sigma = 0.2
    
    # loss weights
    lambda_supervised = 1.0
    lambda_pde = 0.01
    lambda_boundary = 0.1
    
    print(f"training for {num_epochs} epochs.")
    print(f"total parameters: {sum(p.numel() for p in model.parameters() if p.requires_grad)}")
    
    best_loss = float('inf')
    
    for epoch in range(num_epochs):
        
        model.train()
        epoch_loss = 0.0
        
        for batch_idx, (inputs_batch, targets_batch) in enumerate(train_loader):
            optimizer.zero_grad()
            
            loss = total_loss(
                model,
                inputs_batch,
                targets_batch,
                lambda_sup=lambda_supervised,
                lambda_pde=lambda_pde,
                lambda_boundary=lambda_boundary,
                r=r,
                sigma=sigma
            )
            
            loss.backward()
            
            # another technique to prevent gradient exploding, https://arxiv.org/abs/1905.11881
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            
            optimizer.step()
            epoch_loss += loss.item()
        
        avg_epoch_loss = epoch_loss / len(train_loader)
        
        scheduler.step(avg_epoch_loss)
        
        if avg_epoch_loss < best_loss:
            best_loss = avg_epoch_loss
        
        if (epoch + 1) % 50 == 0 or epoch == 0:
            print(f"Epoch [{epoch+1}/{num_epochs}], Loss: {avg_epoch_loss:.6f}, LR: {optimizer.param_groups[0]['lr']:.2e}")
    
    print(f"Training finished. Best loss: {best_loss:.6f}")
    return model


# Test the model
def test_model(model):
    print("\n=== Model Testing ===")
    
    test_cases = [
        (80.0, 100.0, 0.5),
        (90.0, 100.0, 0.5),
        (100.0, 100.0, 0.5),
        (110.0, 100.0, 0.5),
        (120.0, 100.0, 0.5)
    ]
    
    test_inputs = []
    for S, K, T in test_cases:
        test_inputs.append([S/K, K/100.0, T])
    
    test_S = torch.tensor(test_inputs, dtype=torch.float32)
    
    model.eval()
    with torch.no_grad():
        prices = model(test_S)
        
        print("Monotonicity test (should increase with S):")
        
        for i, ((S, K, T), price) in enumerate(zip(test_cases, prices)):
            print(f"S={S:.0f}: C={price.item():.4f}")
    
    
    print("\nComparison with Black-Scholes:")
    test_cases_bs = [
        (100, 100, 0.25),  # ATM, short term
        (100, 100, 1.0),   # ATM, long term
        (120, 100, 0.5),   # ITM
        (80, 100, 0.5),    # OTM
    ]
    
    for S, K, T in test_cases_bs:
        bs_price = black_scholes_call_price(S, K, T, 0.05, 0.2)
        test_input = torch.tensor([[S/K, K/100.0, T]], dtype=torch.float32)
        
        with torch.no_grad():
            nn_price = model(test_input).item()
        
        error = abs(nn_price - bs_price) / bs_price * 100
        print(f"S={S}, K={K}, T={T}: BS={bs_price:.4f}, NN={nn_price:.4f}, Error={error:.2f}%")

if __name__ == "__main__":
    
    model = train_model()
    test_model(model)