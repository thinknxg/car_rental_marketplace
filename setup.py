from setuptools import setup, find_packages

setup(
    name="rental_marketplace",
    version="1.1.0",
    description="Tender/bid-based car rental marketplace on Frappe v16",
    author="Kreatao",
    author_email="dev@kreatao.com",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=["requests"],
)
