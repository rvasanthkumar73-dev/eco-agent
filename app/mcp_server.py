from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Eco MCP Server")

@mcp.tool()
def get_carbon_coefficients() -> dict:
    """Get carbon coefficients for various activities in kg CO2 per unit.
    
    Returns:
        A dictionary containing transit coefficients (kg CO2 per km) and meal coefficients (kg CO2 per meal).
    """
    return {
        "transit": {
            "car": 0.18,      # kg CO2 per km
            "bus": 0.08,      # kg CO2 per km
            "train": 0.04,    # kg CO2 per km
            "flight": 0.25,   # kg CO2 per km
            "bike": 0.0,      # kg CO2 per km
            "walk": 0.0       # kg CO2 per km
        },
        "meal": {
            "beef": 15.5,     # kg CO2 per meal
            "chicken": 3.0,   # kg CO2 per meal
            "vegetarian": 1.2, # kg CO2 per meal
            "vegan": 0.7,      # kg CO2 per meal
            "dairy": 2.0       # kg CO2 per meal
        }
    }

@mcp.tool()
def calculate_emissions(activity_type: str, subtype: str, quantity: float) -> float:
    """Calculate the carbon footprint for a specific activity.
    
    Args:
        activity_type: The type of activity ('transit' or 'meal').
        subtype: The specific subtype (e.g. 'car', 'bus', 'beef', 'vegetarian').
        quantity: The quantity (distance in km for transit, number of meals for meal).
        
    Returns:
        The computed emissions in kg CO2.
    """
    coeffs = get_carbon_coefficients()
    if activity_type not in coeffs:
        return 0.0
    if subtype not in coeffs[activity_type]:
        return 0.0
    return coeffs[activity_type][subtype] * quantity

@mcp.tool()
def get_green_alternatives(activity_type: str, current_subtype: str) -> dict:
    """Get lower-carbon footprint alternatives for a given activity.
    
    Args:
        activity_type: The type of activity ('transit' or 'meal').
        current_subtype: The current subtype used (e.g., 'car', 'beef').
        
    Returns:
        A dictionary containing the current footprint and a list of alternative subtypes with percentage savings.
    """
    coeffs = get_carbon_coefficients()
    if activity_type not in coeffs or current_subtype not in coeffs[activity_type]:
        return {"alternatives": [], "message": "Unknown activity or subtype."}
    
    current_val = coeffs[activity_type][current_subtype]
    alternatives = []
    
    for alt, val in coeffs[activity_type].items():
        if val < current_val:
            alternatives.append({
                "subtype": alt,
                "coefficient": val,
                "saving_pct": round((current_val - val) / current_val * 100, 1) if current_val > 0 else 0.0
            })
            
    # Sort by lowest coefficient (greenest)
    alternatives.sort(key=lambda x: x["coefficient"])
    
    return {
        "current": {"subtype": current_subtype, "coefficient": current_val},
        "alternatives": alternatives
    }

@mcp.tool()
def get_offset_options() -> list:
    """Get standard carbon offsetting methods and their carbon reduction details.
    
    Returns:
        A list of dicts detailing offset projects and their cost per ton of CO2 offset.
    """
    return [
        {"name": "Tree Planting", "description": "Afforestation projects absorbing CO2.", "cost_per_ton_co2": 15.0},
        {"name": "Wind Energy", "description": "Funding renewable wind power installations.", "cost_per_ton_co2": 10.0},
        {"name": "Methane Capture", "description": "Capturing methane from landfills for energy.", "cost_per_ton_co2": 12.0},
        {"name": "Solar Cookstoves", "description": "Providing clean cookstoves to reduce biomass burning.", "cost_per_ton_co2": 8.0}
    ]

if __name__ == "__main__":
    mcp.run()
