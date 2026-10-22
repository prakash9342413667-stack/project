
import tkinter as tk
from tkinter import ttk, messagebox

# Calculate electricity bill
def calculate_bill():
    customer_id = id_entry.get().strip()
    name = name_entry.get().strip()
    category = category_box.get()

    try:
        units = int(units_entry.get())
    except ValueError:
        messagebox.showerror("Error", "Enter valid electricity units!")
        return

    if customer_id == "" or name == "":
        messagebox.showerror("Error", "Enter Customer ID and Name!")
        return

    if units < 0:
        messagebox.showerror("Error", "Units cannot be negative!")
        return

    bill = 0

    # Domestic electricity calculation
    if category == "Domestic":
        if units > 100:
            bill += min(units, 200) - 100
            bill -= min(units, 200) - 100
            bill += (min(units, 200) - 100) * 2.35

        if units > 200:
            bill += (min(units, 400) - 200) * 4.70

        if units > 400:
            bill += (min(units, 500) - 400) * 6.30

        if units > 500:
            bill += (min(units, 600) - 500) * 8.40

        if units > 600:
            bill += (min(units, 800) - 600) * 9.45

        if units > 800:
            bill += (min(units, 1000) - 800) * 10.50

        if units > 1000:
            bill += (units - 1000) * 11.55

        tax = 0

    # Commercial electricity calculation
    else:
        rate = 8.00
        bill = units * rate
        tax = bill * 0.05
        bill = bill + tax

    # Display bill
    result_label.config(
        text=(
            "ELECTRICITY BILL\n\n"
            f"Customer ID : {customer_id}\n"
            f"Customer Name : {name}\n"
            f"Category : {category} Use\n"
            f"Units Consumed : {units}\n"
            f"Energy Charge : Rs. {bill - tax:.2f}\n"
            f"Electricity Tax : Rs. {tax:.2f}\n"
            f"Total Bill : Rs. {bill:.2f}"
        )
    )


# Clear fields
def clear_fields():
    id_entry.delete(0, tk.END)
    name_entry.delete(0, tk.END)
    units_entry.delete(0, tk.END)
    category_box.set("Domestic")
    result_label.config(text="")


# Main window
root = tk.Tk()
root.title("Electricity Bill Calculator")
root.configure(bg="#eaf2f8")
root.attributes("-fullscreen", True)

root.bind(
    "<Escape>",
    lambda event: root.attributes("-fullscreen", False)
)

# Heading
tk.Label(
    root,
    text="ELECTRICITY BILL CALCULATOR",
    font=("Arial", 28, "bold"),
    bg="#1f4e79",
    fg="white",
    pady=22
).pack(fill="x")

# Input form
form = tk.Frame(root, bg="#eaf2f8")
form.pack(pady=25)

label_font = ("Arial", 17)
entry_font = ("Arial", 17)

# Customer ID
tk.Label(
    form, text="Customer ID:",
    font=label_font, bg="#eaf2f8"
).grid(row=0, column=0, padx=20, pady=10, sticky="w")

id_entry = tk.Entry(form, font=entry_font, width=25)
id_entry.grid(row=0, column=1, padx=20, pady=10)

# Customer Name
tk.Label(
    form, text="Customer Name:",
    font=label_font, bg="#eaf2f8"
).grid(row=1, column=0, padx=20, pady=10, sticky="w")

name_entry = tk.Entry(form, font=entry_font, width=25)
name_entry.grid(row=1, column=1, padx=20, pady=10)

# Electricity category
tk.Label(
    form, text="Electricity Type:",
    font=label_font, bg="#eaf2f8"
).grid(row=2, column=0, padx=20, pady=10, sticky="w")

category_box = ttk.Combobox(
    form,
    values=["Domestic", "Commercial"],
    state="readonly",
    font=entry_font,
    width=23
)
category_box.grid(row=2, column=1, padx=20, pady=10)
category_box.set("Domestic")

# Units consumed
tk.Label(
    form, text="Units Consumed:",
    font=label_font, bg="#eaf2f8"
).grid(row=3, column=0, padx=20, pady=10, sticky="w")

units_entry = tk.Entry(form, font=entry_font, width=25)
units_entry.grid(row=3, column=1, padx=20, pady=10)

# Buttons
button_frame = tk.Frame(root, bg="#eaf2f8")
button_frame.pack(pady=10)

tk.Button(
    button_frame,
    text="Calculate Bill",
    command=calculate_bill,
    font=("Arial", 15, "bold"),
    bg="#2980b9",
    fg="white",
    width=15,
    padx=8,
    pady=8
).grid(row=0, column=0, padx=10)

tk.Button(
    button_frame,
    text="Clear",
    command=clear_fields,
    font=("Arial", 15),
    width=10,
    padx=8,
    pady=8
).grid(row=0, column=1, padx=10)

tk.Button(
    button_frame,
    text="Exit Full Screen",
    command=lambda: root.attributes("-fullscreen", False),
    font=("Arial", 15),
    width=16,
    padx=8,
    pady=8
).grid(row=0, column=2, padx=10)

# Bill display
result_label = tk.Label(
    root,
    text="Enter customer details and calculate the bill",
    font=("Arial", 17, "bold"),
    bg="white",
    fg="#1f4e79",
    justify="left",
    padx=30,
    pady=20,
    relief="groove",
    borderwidth=2
)
result_label.pack(pady=15)

root.mainloop()

